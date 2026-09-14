"""``ctx pack "<task>"`` — a ranked, budgeted, evidence-carrying context pack
for a task, at turn one (docs/CODE-SEARCH.md).

What a coding agent does in its first ten turns is retrieval: grep a term,
read a file, grep another, read the wrong file, read the right one. Each of
those turns re-sends the whole cached prefix and lands a tool result in the
transcript that stays there for the session. A context pack front-loads the
answer to *where would I look*: the files and symbols that the task text,
the symbol table and the history all point at, with the reason each one is
there, in a bounded rendering the model can open by handle.

Signals (all from things ctx already has; nothing is embedded, nothing is
learned):

* **terms** — identifiers and words from the task, weighted by rarity across
  the corpus (idf from the trigram index, which is why the pack costs
  milliseconds and not a corpus scan) and by how code-like they are
  (``CamelCase``/``snake_case`` and backticked spans count more).
* **symbols** — a term that *names* a symbol in a file is worth more than a
  term that appears in it; the symbol table lives in the same index.
* **paths** — a term in the path (``converters`` for ``converters.py``).
* **history** — commits whose message mentions the rarest terms; the files
  they touched carry the commit as evidence, newest first.
* **shape** — test files are demoted unless the task is about tests; very
  large files are mildly normalized so a hub that mentions everything does
  not win on length alone.

Every row says *why* it is in the pack (its terms, symbol, commit), so the
model can disagree with the ranking on evidence rather than on faith, and
the outline under it is the file's own skeleton — the next read is a range
or a symbol, never the first page.

Staleness: the index is synced before ranking (fingerprint sweep), the
outlines come from the current bytes, and the pack is deterministic for a
tree state — re-run it after a large change, it costs the same.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from ctx.store import Store
from ctx.workspace import Workspace

DEFAULT_BUDGET_TOKENS = 2500
DEFAULT_MAX_FILES = 8
_HISTORY_COMMITS = 30
_MAX_TERMS = 24
_OUTLINE_SYMBOLS = 6
_TEST_DEMOTION = 0.6
_PROSE_DEMOTION = 0.5
_SMALL_CORPUS = 25  # below this every term is worth verifying
_HISTORY_WEIGHT = 0.1
_CONFIG_RE = re.compile(r"\.(toml|cfg|ini|yaml|yml|json)$|(^|/)(setup\.py|Makefile|Dockerfile)$")

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CODE_SPAN_RE = re.compile(r"`([^`\n]{2,80})`")
_PATH_RE = re.compile(r"\b(?:[\w.-]+/)+[\w.-]+\.\w{1,6}\b")
_CAMEL_RE = re.compile(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+|\d+")

_STOP = frozenset("""
the and for with that this from into onto when then than they them their there
should would could must may might can will shall also both each such only same
about above after again before being between does doing done during either
every here more most other over some under until upon very where which while
whose your ours these those what have has had were was are our not but all any
add adds added adding new use uses used using make makes made making set sets
get gets got need needs needed want wants like just via per one two three
implement implements implementing implementation support supports supported
supporting ensure ensures provide provides providing return returns returned
returning value values object objects field fields name names type types
true false none null list dict string number function functions method methods
class classes module modules file files code call calls called caller callers
input output inputs outputs case cases given following include includes
including without within default defaults option options optional current
existing example examples etc def self args kwargs int str bool float
""".split())

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|testing|spec|specs|__tests__)(/|$)|(^|/)test_[^/]*$|_test\.\w+$|\.spec\.\w+$")


@dataclass
class Term:
    text: str            # as written (for symbol/path matching)
    key: str             # lowercased lookup key
    weight: float        # from where it came in the task text
    df: int = 0
    idf: float = 0.0

    @property
    def score(self) -> float:
        return self.weight * self.idf


@dataclass
class PackRow:
    rel: str
    score: float
    terms: list[str] = field(default_factory=list)
    symbols: list[tuple[str, str, int]] = field(default_factory=list)  # (name, kind, line)
    path_terms: list[str] = field(default_factory=list)
    commits: list[tuple[str, str]] = field(default_factory=list)       # (sha, subject)
    outline: list[str] = field(default_factory=list)
    lines: int = 0
    size: int = 0
    lang: str | None = None
    symbol_count: int = 0

    def why(self) -> str:
        parts: list[str] = []
        if self.terms:
            parts.append("terms " + ", ".join(self.terms[:5]) + (f" (+{len(self.terms) - 5})" if len(self.terms) > 5 else ""))
        if self.symbols:
            parts.append("defines " + ", ".join(f"{n}" for n, _k, _l in self.symbols[:3]))
        if self.path_terms:
            parts.append("path " + ", ".join(self.path_terms[:2]))
        if self.commits:
            sha, subject = self.commits[0]
            parts.append(f"commit {sha} \"{subject[:48]}\"")
        return " · ".join(parts)

    def payload(self) -> dict[str, Any]:
        return {
            "file": self.rel, "score": round(self.score, 3), "terms": self.terms,
            "symbols": [list(s) for s in self.symbols], "path_terms": self.path_terms,
            "commits": [list(c) for c in self.commits], "lines": self.lines, "size": self.size,
            "language": self.lang, "symbol_count": self.symbol_count, "outline": self.outline,
        }


@dataclass
class Pack:
    task: str
    terms: list[Term]
    rows: list[PackRow]
    corpus_files: int
    index_note: str
    history: list[tuple[str, str, str]]   # (sha, date, subject)
    budget_tokens: int
    considered: int

    def payload(self) -> dict[str, Any]:
        return {
            "schema": "ctx.pack/v1",
            "task": self.task[:400],
            "terms": [{"text": t.text, "weight": round(t.weight, 2), "df": t.df, "idf": round(t.idf, 3)}
                      for t in self.terms],
            "files": [r.payload() for r in self.rows],
            "history": [list(h) for h in self.history],
            "corpus_files": self.corpus_files, "considered": self.considered,
            "budget_tokens": self.budget_tokens, "index": self.index_note,
        }


# ------------------------------------------------------------------ terms
def _split_words(ident: str) -> list[str]:
    parts = [p for chunk in ident.split("_") for p in _CAMEL_RE.findall(chunk)]
    return [p for p in parts if len(p) >= 3 and not p.isdigit()]


def extract_terms(task: str, *, max_terms: int = _MAX_TERMS) -> tuple[list[Term], list[str]]:
    """Weighted terms from the task text, and any paths it names.

    Weights: a backticked span's identifiers 2.0; an identifier that is
    code-shaped (CamelCase, snake_case, or contains a digit) 1.5; a plain
    word 1.0; the sub-words of a compound identifier 0.6 (they find the
    file whose name shares a word, without out-voting the whole name).
    Repeats add a little (log), never linearly — a task that says
    "converter" nine times is not nine times about converters."""
    weights: dict[str, float] = {}
    spelling: dict[str, str] = {}
    counts: dict[str, int] = {}

    def add(tok: str, w: float) -> None:
        key = tok.lower()
        if key in _STOP or len(key) < 3:
            return
        counts[key] = counts.get(key, 0) + 1
        if w > weights.get(key, 0.0):
            weights[key] = w
            spelling[key] = tok
        elif key not in spelling:
            spelling[key] = tok

    paths = list(dict.fromkeys(_PATH_RE.findall(task)))
    for span in _CODE_SPAN_RE.findall(task):
        for tok in _IDENT_RE.findall(span):
            add(tok, 2.0)
            for sub in _split_words(tok):
                if sub.lower() != tok.lower():
                    add(sub, 0.6)
    for tok in _IDENT_RE.findall(task):
        code_shaped = "_" in tok or any(ch.isdigit() for ch in tok) or (
            tok[0].isupper() and any(ch.islower() for ch in tok[1:]) and any(ch.isupper() for ch in tok[1:])
        ) or (tok[0].islower() and any(ch.isupper() for ch in tok))
        add(tok, 1.5 if code_shaped else 1.0)
        if code_shaped:
            for sub in _split_words(tok):
                if sub.lower() != tok.lower():
                    add(sub, 0.6)
    terms = [
        Term(text=spelling[k], key=k, weight=w * (1.0 + 0.25 * math.log(counts[k])))
        for k, w in weights.items()
    ]
    terms.sort(key=lambda t: (-t.weight, t.key))
    return terms[:max_terms], paths


# ---------------------------------------------------------------- ranking
def _history_evidence(ws: Workspace, terms: list[Term], *, max_count: int) -> tuple[
    dict[str, list[tuple[str, str, int]]], list[tuple[str, str, str]]
]:
    """files → [(sha, subject, files touched)] from commits whose message
    mentions any of the rarest terms (git ORs multiple --grep), newest
    first."""
    import os
    import subprocess

    if ws.git is None or not terms:
        return {}, []
    argv = ["git", "log", f"--max-count={max_count}", "--date=short", "--no-merges",
            "--regexp-ignore-case", "--fixed-strings",
            "--format=%x1e%h%x1f%ad%x1f%s", "--name-only"]
    for t in terms:
        argv += ["--grep", t.text]
    try:
        out = subprocess.run(argv, cwd=str(ws.root), capture_output=True, text=True, timeout=15,
                             env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout
    except (OSError, subprocess.SubprocessError):
        return {}, []
    by_file: dict[str, list[tuple[str, str, int]]] = {}
    commits: list[tuple[str, str, str]] = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, files = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 3:
            continue
        sha, date, subject = parts[0], parts[1], parts[2]
        commits.append((sha, date, subject))
        touched = [f.strip() for f in files.splitlines() if f.strip()]
        for f in touched:
            by_file.setdefault(f, []).append((sha, subject, len(touched)))
    return by_file, commits


def build_pack(
    store: Store, ws: Workspace, task: str, *,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS, max_files: int = DEFAULT_MAX_FILES,
    history: bool = True,
) -> Pack:
    from ctx import codeindex

    idx = codeindex.open_index(store, ws, build=True)
    if idx is None:
        raise RuntimeError("the text index is off (CTX_SEARCH_INDEX=off); ctx pack needs it")
    try:
        terms, named_paths = extract_terms(task)
        n_files = len(idx.files) if idx.files else 1
        about_tests = any(t.key in ("test", "tests", "testing", "pytest") for t in terms)

        scores: dict[str, float] = {}
        hits: dict[str, list[str]] = {}
        sym_hits: dict[str, list[tuple[str, str, int]]] = {}
        path_hits: dict[str, list[str]] = {}

        # terms: trigram candidates, then verified against the bytes — a
        # candidate is a file that *may* contain the term; an SVG or a
        # minified bundle is a candidate for everything. Presence is a
        # whole-word occurrence count (tf), so a file that uses a name
        # twelve times outranks a changelog that mentions it once.
        texts: dict[str, bytes] = {}
        about_docs = any(t.key in ("docs", "documentation", "readme", "changelog", "docstring") for t in terms)
        for t in terms:
            cands = idx.candidates(codeindex.expr_for_literal(t.key))
            if not cands or (n_files >= _SMALL_CORPUS and len(cands) > 0.4 * n_files):
                t.df = len(cands)
                t.idf = 0.0  # says nothing about where to look
                continue
            rx = re.compile(rb"(?<![a-z0-9_])" + re.escape(t.key.encode()) + rb"(?![a-z0-9_])")
            tfs: dict[str, int] = {}
            for rel in cands:
                data = texts.get(rel)
                if data is None:
                    try:
                        data = texts[rel] = (ws.root / rel).read_bytes().lower()
                    except OSError:
                        texts[rel] = b""
                        continue
                n = len(rx.findall(data))
                if n:
                    tfs[rel] = n
            t.df = len(tfs)
            t.idf = math.log(1.0 + n_files / (1.0 + t.df))
            if not tfs or t.idf <= 0.05:
                continue
            for rel, n in tfs.items():
                scores[rel] = scores.get(rel, 0.0) + t.score * (1.0 + math.log(n))
                hits.setdefault(rel, []).append(t.text)

        # symbols: a term that names something is the strongest signal
        for t in terms:
            if len(t.key) < 4:
                continue
            defs = idx.files_defining(t.text) or _defining_ci(idx, t.key)
            for rel, kind, line in defs[:20]:
                bonus = 3.0 * t.weight * max(t.idf, 0.5)
                if not any(s[0] == t.text for s in sym_hits.get(rel, [])):
                    scores[rel] = scores.get(rel, 0.0) + bonus
                    sym_hits.setdefault(rel, []).append((t.text, kind, line))

        # paths: the task named a file, or a term is a path component
        for rel in idx.files:
            low = rel.lower()
            for t in terms:
                if len(t.key) >= 4 and t.key in low.replace("_", "").replace("-", "") + low:
                    scores[rel] = scores.get(rel, 0.0) + 1.5 * t.weight * max(t.idf, 0.5)
                    path_hits.setdefault(rel, []).append(t.text)
        for p in named_paths:
            for rel in idx.files:
                if rel == p or rel.endswith("/" + p):
                    scores[rel] = scores.get(rel, 0.0) + 8.0
                    path_hits.setdefault(rel, []).insert(0, p)

        # history: commits whose message names the task's strongest terms.
        # Scaled to the term scores (a fixed constant was a tie-breaker that
        # never moved a ranking): one focused commit is worth a tenth of the
        # best term score, a forty-file commit a fraction of that.
        commit_hits: dict[str, list[tuple[str, str]]] = {}
        commits: list[tuple[str, str, str]] = []
        if history:
            rare = [t for t in sorted(terms, key=lambda t: -t.score) if t.idf > 0.3 and len(t.key) >= 5][:8]
            raw_hits, commits = _history_evidence(ws, rare, max_count=_HISTORY_COMMITS)
            top_score = max(scores.values()) if scores else 1.0
            for rel, cs in raw_hits.items():
                if rel not in idx.files:
                    continue
                boost = sum(1.0 / (1.0 + math.log(1.0 + n)) for _sha, _subject, n in cs[:3])
                scores[rel] = scores.get(rel, 0.0) + _HISTORY_WEIGHT * top_score * boost
                commit_hits[rel] = [(sha, subject) for sha, subject, _n in cs]

        # shape: tests and prose demoted, size normalized
        rows: list[PackRow] = []
        for rel, sc in scores.items():
            ent = idx.files.get(rel)
            size = int(ent[2]) if ent else 0
            if _TEST_PATH_RE.search(rel) and not about_tests:
                sc *= _TEST_DEMOTION
            if ent and ent[5] is None and not about_docs and not _CONFIG_RE.search(rel):
                sc *= _PROSE_DEMOTION  # docs are evidence, rarely the fix
            sc /= 1.0 + 0.15 * math.log(1.0 + size / 20000.0)
            rows.append(PackRow(
                rel=rel, score=sc, terms=hits.get(rel, []), symbols=sym_hits.get(rel, []),
                path_terms=path_hits.get(rel, []), commits=commit_hits.get(rel, [])[:3],
                size=size, lines=int(ent[6]) if ent else 0, lang=ent[5] if ent else None,
            ))
        rows.sort(key=lambda r: (-r.score, r.rel))
        considered = len(rows)
        rows = rows[:max_files]

        # outlines from the current bytes, matched symbols first
        for r in rows:
            r.outline, r.symbol_count = _outline(idx, r)
        note = f"index trigram · {len(idx.files)} files"
    finally:
        idx.close()
    return Pack(task=task, terms=terms, rows=rows, corpus_files=n_files, index_note=note,
                history=commits[:5], budget_tokens=budget_tokens, considered=considered)


def _defining_ci(idx, key: str) -> list[tuple[str, str, int]]:
    """Case-insensitive exact symbol matches (``converter`` finds
    ``Converter``); never substring, which made ``tool`` claim every
    ``_tool_*`` helper as a definition."""
    return [(rel, kind, line) for rel, kind, line in idx.files_defining(key, exact=False)
            if rel and _sym_name(idx, rel, line, key)]


def _sym_name(idx, rel: str, line: int, key: str) -> bool:
    for name, _kind, ln in idx.symbols(rel):
        if int(ln) == line and str(name).lower() == key:
            return True
    return False


def _outline(idx, row: PackRow) -> tuple[list[str], int]:
    syms = idx.symbols(row.rel)
    if not syms:
        return [], 0
    named = {n.lower() for n, _k, _l in row.symbols}
    long_terms = [t.lower() for t in row.terms if len(t) >= 6]
    matched = [s for s in syms if str(s[0]).lower() in named
               or any(w in str(s[0]).lower() for w in long_terms)]
    rest = [s for s in syms if s not in matched]
    shown = matched[:_OUTLINE_SYMBOLS] + rest[: max(0, _OUTLINE_SYMBOLS - len(matched))]
    shown.sort(key=lambda s: int(s[2]))
    lines = [f"{kind} {name} L{line}" for name, kind, line in shown]
    if len(syms) > len(shown):
        lines.append(f"+{len(syms) - len(shown)} more symbols")
    return lines, len(syms)


# --------------------------------------------------------------- rendering
def _approx_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def render_pack(pack: Pack) -> str:
    from ctx.textutil import fmt_int

    title = " ".join(pack.task.split())[:72]
    out: list[str] = [f"[ctx pack · {len(pack.rows)} of {fmt_int(pack.considered)} candidate files · {pack.index_note}]"]
    out.append(f"task: {title}{'…' if len(pack.task) > 72 else ''}")
    shown_terms = [t for t in pack.terms if t.idf > 0.05][:10]
    out.append("terms: " + " ".join(f"{t.text}({t.idf:.1f})" for t in shown_terms))
    used = _approx_tokens("\n".join(out))
    for i, r in enumerate(pack.rows, start=1):
        block = [f"{i}. repo:{r.rel} · {fmt_int(r.lines)} lines · score {r.score:.1f}"]
        why = r.why()
        if why:
            block.append(f"   why: {why}")
        for ln in r.outline:
            block.append(f"   {ln}")
        cost = _approx_tokens("\n".join(block))
        if used + cost > pack.budget_tokens and i > 3:
            out.append(f"+{len(pack.rows) - i + 1} more files within the candidates; raise --budget or ask for them: "
                       + ", ".join(x.rel for x in pack.rows[i - 1:i + 2]))
            break
        out.extend(block)
        used += cost
    if pack.history:
        out.append("history:")
        for sha, date, subject in pack.history[:3]:
            out.append(f"  {sha} {date} · {subject[:80]}")
    out.append("next:")
    if pack.rows:
        top = pack.rows[0]
        sym = top.symbols[0][0] if top.symbols else None
        if sym:
            out.append(f"  ctx get repo:{top.rel} --symbol {sym}")
        else:
            out.append(f"  ctx stats repo:{top.rel}")
    if shown_terms:
        out.append("  ctx search repo: " + " ".join(f"'{t.text}'" for t in shown_terms[:2]) + " --all")
    return "\n".join(out)
