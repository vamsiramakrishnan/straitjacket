"""ContextBench as teacher: does the search lane reach the gold context?

ContextBench (arXiv:2602.05892, Li et al.) is the first public corpus that
labels *what an agent should have looked at*, not just whether it fixed the
bug: 1,136 issue-resolution tasks over 66 repositories and eight languages,
each carrying human-annotated gold context as ``{file, start_line,
end_line}`` blocks. The 500-instance ``contextbench_verified`` subset is the
one this runner uses by default.

Why this corpus and not another. ``evals/swe_learn.py`` scores the *output*
channel: it reproduces a real failure and asks whether the digest surfaced
the gold files. Its standing finding is that most gold evidence is
**not-in-output** — no digest of a test run could ever have delivered it,
because it lives in source the agent has to go find. That residue is the
search lane's territory (``ctx map``, ``ctx search``, ``ctx def``,
``ctx refs``), and until now the repository had no ground truth to referee
it. `BENCHMARK.md` reserves the slot ("SJ-Explore-60 ... adopted contingent
on verification ... line-level gold regions"). ContextBench fills it, and
the dataset's shape was verified against the actual release before this
file was written: 500 rows, 58 repos, 8 languages, 4,597 gold blocks over
1,746 files, gold blocks carrying real line coordinates.

The house rule still holds: **external corpora are teachers, never
referees.** Nothing here produces a leaderboard number or a comparison
against the agents in the paper. What it produces is a defect queue --- gold
blocks the deterministic search lane failed to reach, addressed well enough
to fix a ranking rule or a verb.

What is measured
----------------
A deterministic, model-free retrieval policy (``searchlane``) is
driven from the issue text alone through the real ``ctx`` CLI, and its
result is scored against gold at the paper's three granularities:

* **file** — do the retrieved paths match the gold paths?
* **block** — is a gold definition block covered (``--block-overlap``, default
  0.5 of its lines)?
* **line** — line-set overlap, the strictest view.

Each reports recall, precision and F1. Because precision is meaningless
without a cost, every arm also reports the **retrieved token budget** actually
spent, and the runner sweeps a budget ladder (``--budgets``) so the output is
a recall-vs-tokens curve rather than one collapsed score.

Honest limits, stated once
--------------------------
1. The paper's evaluation harness is not public. File- and line-level
   scoring here follows the paper's stated definitions directly. Block-level
   alignment is an **adaptation**: the paper aligns AST definition nodes,
   this runner counts a gold block as retrieved when a retrieved span covers
   at least ``--block-overlap`` of its lines. Numbers from this file are not
   comparable to the paper's tables and are never to be published as such.
2. ``searchlane`` is a *deterministic* policy with no model in the
   loop. The paper's numbers come from LLM-driven agents over many turns.
   This measures the floor the verbs deliver unaided, which is the number
   that tells us whether a ranking change helped. It is not an agent score.
3. A gold block is only reachable if the retrieval policy can name it. When
   the policy misses, that is a lane defect and belongs in the queue --- not
   a statement about any model.

Usage
-----
    python evals/contextbench.py --workdir /scratch/cb --limit 12
    python evals/contextbench.py --workdir /scratch/cb --limit 40 --stratify
    python evals/contextbench.py --workdir /scratch/cb --language python \
        --budgets 2000,8000,32000 --json evals/contextbench-<date>.json

The corpus is cached to ``<workdir>/corpus-<config>.jsonl`` on first run; repository
trees are cached per (repo, commit) under ``<workdir>/repos`` as single-commit
shallow fetches (~50 MB each). ``--keep`` retains them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROWS_API = "https://datasets-server.huggingface.co/rows"
DATASET = "Contextbench/ContextBench"
CONFIGS = {"verified": "contextbench_verified", "full": "default"}

# Source extensions we will let a bare path probe match. ContextBench spans
# eight languages; the gold blocks are always source, never build output.
CODE_EXT = (
    "py pyi js jsx ts tsx mjs cjs go rs c h cc cpp cxx hpp hh java kt scala rb "
    "php cs swift m mm svelte vue"
).split()
_EXT_RE = "|".join(CODE_EXT)

# Tokens that appear in every issue and would drown the ranking.
_STOP = {
    "the", "this", "that", "with", "from", "when", "then", "should", "would",
    "there", "which", "here", "have", "been", "does", "also", "into", "such",
    "code", "file", "line", "error", "issue", "bug", "test", "tests", "true",
    "false", "none", "null", "self", "return", "import", "class", "def",
    "function", "value", "values", "using", "used", "does", "will", "your",
    "expected", "actual", "output", "input", "example", "version", "python",
    "traceback", "exception", "print", "string", "object", "type", "types",
    "name", "names", "call", "calls", "case", "cases", "result", "results",
    "problem", "following", "above", "below", "instead", "because", "before",
    "after", "same", "make", "made", "like", "only", "just", "not", "but",
}

_BACKTICK = re.compile(r"`{1,3}([^`]{2,400})`{1,3}", re.S)
_PATH = re.compile(rf"\b((?:[\w.\-]+/)+[\w.\-]+\.(?:{_EXT_RE}))\b")
_PYTRACE = re.compile(r'File "([^"]+)", line (\d+)')
_CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_SNAKE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
_IDENT = re.compile(r"\b([A-Za-z_]\w{3,})\b")

_DEF_SPAN = re.compile(r"definition: repo:(\S+) L(\d+):(\d+)")
_SEARCH_FILE = re.compile(r"^(\S.*?):$")
_SEARCH_HIT = re.compile(r"^\s+L(\d+):")
_REFS_HIT = re.compile(r"^repo:(\S+?):L(\d+):")
# `  L305 → _stream  src/ctx/digest/__init__.py:280` (callees) and
# `    _emission_gate  src/ctx/hook.py:2923` (callers) share a tail shape.
_GRAPH_SITE = re.compile(rf"\s([\w./\-]+\.(?:{_EXT_RE})):(\d+)\s*$")
# Vendored copies of the framework under discussion are the reporter's
# environment, not the repository under test.
_VENDOR = re.compile(r"^.*?(?:site-packages|node_modules|vendor|venv|\.tox)/")

BYTES_PER_TOKEN = 4  # matches ctx.textutil.estimate_tokens


# --------------------------------------------------------------------------
# corpus


def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "straitjacket-bench"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:  # noqa: S310
        return fh.read()


def load_corpus(workdir: Path, config: str, refresh: bool = False) -> list[dict]:
    """Fetch (and cache) the ContextBench table via the datasets-server rows API.

    urllib only, on purpose: this runner must work in a review sandbox with
    nothing installed beyond the repository's own dev extras.
    """
    cache = workdir / f"corpus-{config}.jsonl"
    if cache.exists() and not refresh:
        return [json.loads(l) for l in cache.read_text(encoding="utf-8").splitlines() if l]

    rows: list[dict] = []
    offset, page = 0, 20
    while True:
        q = urllib.parse.urlencode(
            {
                "dataset": DATASET,
                "config": CONFIGS[config],
                "split": "train",
                "offset": offset,
                "length": page,
            }
        )
        try:
            payload = json.loads(_get(f"{ROWS_API}?{q}"))
        except urllib.error.HTTPError as e:  # pragma: no cover - network shape
            raise SystemExit(f"datasets-server refused offset {offset}: {e}") from e
        batch = payload.get("rows", [])
        if not batch:
            break
        rows.extend(r["row"] for r in batch)
        total = payload.get("num_rows_total", 0)
        offset += page
        print(f"  corpus: {len(rows)}/{total}", end="\r", file=sys.stderr, flush=True)
        if offset >= total:
            break
    print(f"  corpus: {len(rows)} instances cached          ", file=sys.stderr)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return rows


_CONTAINER = re.compile(r"^/?(?:workspace|testbed|repo|home/\w+)/[\w.\-]+/")


def normalize_gold_path(raw: str) -> str:
    """Gold paths are not uniformly repo-relative across the four sources.

    Multi-SWE-bench annotations were produced inside a container and carry an
    absolute prefix (``/workspace/facebook__zstd__0.1/programs/fileio.c``);
    SWE-bench-Verified rows are already repo-relative. Charging a runner's
    miss to the lane because of an annotation prefix would be a measurement
    bug, so the prefix is stripped here and the result is re-checked against
    the real tree in `resolve_gold` before anything is scored.
    """
    p = raw.strip().replace("\\", "/")
    p = _CONTAINER.sub("", p)
    return p.lstrip("/").removeprefix("./")


def gold_blocks(inst: dict) -> list[dict]:
    raw = inst.get("gold_context") or "[]"
    if isinstance(raw, str):
        raw = json.loads(raw)
    out = []
    for b in raw:
        try:
            s, e = int(b["start_line"]), int(b["end_line"])
        except (KeyError, TypeError, ValueError):
            continue
        if s < 1 or e < s:
            continue
        out.append({"file": normalize_gold_path(b["file"]), "start": s, "end": e})
    return out


def resolve_gold(gold: list[dict], root: Path) -> tuple[list[dict], list[str]]:
    """Bind each gold path to a real file in the checked-out tree.

    Exact hit first; then a unique suffix match, which recovers a prefix
    shape the normalizer does not know. A path that still does not resolve
    is **annotation drift**: reported, and excluded from scoring, because a
    file that is not in the tree at base_commit cannot be retrieved from it.
    """
    index: dict[str, list[str]] | None = None
    bound, drift = [], []
    for b in gold:
        if (root / b["file"]).is_file():
            bound.append(b)
            continue
        if index is None:
            index = {}
            for p in root.rglob("*"):
                if p.is_file() and ".git/" not in str(p):
                    index.setdefault(p.name, []).append(
                        str(p.relative_to(root)).replace("\\", "/")
                    )
        tail = b["file"].rsplit("/", 1)[-1]
        cands = [c for c in index.get(tail, []) if c.endswith(b["file"].split("/", 1)[-1])]
        if len(cands) != 1:
            cands = index.get(tail, [])
        if len(cands) == 1:
            bound.append({**b, "file": cands[0]})
        else:
            drift.append(b["file"])
    return bound, sorted(set(drift))


def _dispersion(blocks: list[dict]) -> str:
    """The paper's difficulty frame, reduced to three buckets we can stratify on."""
    files = {b["file"] for b in blocks}
    if len(files) <= 1:
        return "single-file"
    if len(files) <= 3:
        return "multi-file"
    return "dispersed"


def stratified(rows: list[dict], limit: int, seed: int = 0) -> list[dict]:
    """Balanced pick: proportional by language, spread over edit-dispersion.

    Deterministic — the sort key is the instance id, never a RNG, so a rerun
    of the same limit scores the same instances.
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["language"], _dispersion(gold_blocks(r)))
        buckets.setdefault(key, []).append(r)
    for v in buckets.values():
        v.sort(key=lambda r: r["instance_id"])

    picked: list[dict] = []
    order = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    i = 0
    while len(picked) < limit and any(buckets.values()):
        key = order[i % len(order)]
        if buckets[key]:
            picked.append(buckets[key].pop(0))
        i += 1
        if i > len(order) * limit + len(order):
            break
    return picked[:limit]


# --------------------------------------------------------------------------
# repository materialization


def materialize(inst: dict, repos: Path) -> Path | None:
    """Single-commit shallow fetch of the repo at base_commit.

    A blobless or full clone of django/django to read one tree is waste; a
    depth-1 fetch of the exact SHA is ~2s and ~50 MB.
    """
    dest = repos / f"{inst['repo'].replace('/', '__')}@{inst['base_commit'][:12]}"
    if (dest / ".git").exists() and any(dest.iterdir()):
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    url = inst.get("repo_url") or f"https://github.com/{inst['repo']}.git"
    try:
        subprocess.run(["git", "init", "-q", "."], cwd=dest, check=True, timeout=60)
        subprocess.run(
            ["git", "remote", "add", "origin", url], cwd=dest, check=True, timeout=60
        )
        subprocess.run(
            ["git", "fetch", "-q", "--depth", "1", "origin", inst["base_commit"]],
            cwd=dest, check=True, timeout=900,
        )
        subprocess.run(
            ["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest, check=True, timeout=300
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  unavailable: {type(e).__name__} fetching {inst['base_commit'][:12]}")
        shutil.rmtree(dest, ignore_errors=True)
        return None
    # ctx wants a workspace marker; the git root alone is enough, but an
    # explicit ctx.toml keeps resolution off the surrounding scratch tree.
    (dest / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return dest


# --------------------------------------------------------------------------
# probes: issue text -> ranked retrieval terms


@dataclass
class Probes:
    paths: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    anchors: list[tuple[str, int]] = field(default_factory=list)  # (path, line)
    starved: bool = False  # no code-shaped probe in the issue at all


# Prose fallback. Some issues carry no identifier, no path and no traceback --
# "Restrict mutable tuple recovery. This is a fix for #1123." A model-free
# policy has nothing to anchor on there, and reporting 0.0 without saying why
# would blame the lane for the corpus. These become stem probes and the
# instance is flagged `probe_starved` in the record.
_PROSE_MIN = 5
_PROSE_STOP = _STOP | {
    "fix", "fixes", "fixed", "allow", "allows", "allowed", "add", "adds",
    "added", "now", "some", "while", "sure", "good", "thing", "completely",
    "could", "would", "keeping", "combinations", "edit", "this", "these",
}


def _stems(text: str, limit: int) -> list[str]:
    """Content words from the issue title and first paragraph, as stems.

    A stem, not the word: an issue that says "recovery" is looking for code
    that says `recover`, `recovered`, `recover_tuple`. `ctx search` takes a
    regex, so the stem is the pattern.
    """
    head = "\n".join((text or "").strip().splitlines()[:6])
    counts: dict[str, int] = {}
    for w in re.findall(r"\b[A-Za-z][A-Za-z\-]{3,}\b", head):
        lw = w.lower()
        if lw in _PROSE_STOP or len(lw) < 5:
            continue
        stem = re.sub(r"(?:ing|ed|es|s|ion|ions|ity|ies)$", "", lw)
        if len(stem) < 4:
            stem = lw
        counts[stem] = counts.get(stem, 0) + 1
    return sorted(counts, key=lambda s: (-counts[s], -len(s), s))[:limit]


def extract_probes(text: str, max_symbols: int = 12) -> Probes:
    """Rank retrieval terms out of the issue body, deterministically.

    Weighting reflects what actually locates code in an issue: an explicit
    path beats a traceback frame beats a backticked identifier beats prose.
    No model, no embedding — this must produce the same probes on every run.
    """
    text = text or ""
    paths, anchors = [], []
    for m in _PYTRACE.finditer(text):
        p = m.group(1).lstrip("./")
        anchors.append((p, int(m.group(2))))
        paths.append(p)
    paths.extend(m.group(1) for m in _PATH.finditer(text))

    scores: dict[str, float] = {}

    def bump(tok: str, w: float) -> None:
        tok = tok.strip("._-")
        if len(tok) < 4 or tok.lower() in _STOP or tok.isdigit():
            return
        scores[tok] = scores.get(tok, 0.0) + w

    for m in _BACKTICK.finditer(text):
        span = m.group(1)
        for rx, w in ((_CAMEL, 3.0), (_SNAKE, 3.0), (_DOTTED, 2.5)):
            for mm in rx.finditer(span):
                bump(mm.group(1), w)
        # A short backticked span is usually the identifier itself.
        if len(span) <= 60:
            for mm in _IDENT.finditer(span):
                bump(mm.group(1), 1.5)

    for rx, w in ((_CAMEL, 1.2), (_SNAKE, 1.0), (_DOTTED, 0.8)):
        for mm in rx.finditer(text):
            bump(mm.group(1), w)

    # Dotted probes are useful whole and in parts: `frame.attributes.Attribute`
    # locates the module, `Attribute` locates the class.
    for tok in list(scores):
        if "." in tok:
            for part in tok.split("."):
                bump(part, 0.4)

    ranked = sorted(scores, key=lambda t: (-scores[t], t))
    # Starved means *nothing code-shaped at all*, not merely few probes. An
    # earlier threshold of five let prose stems outrank `ProjectState` on a
    # django issue that named its class outright, and the stems dragged the
    # expansion stage onto unrelated files. Stems are a last resort and never
    # displace a real identifier.
    starved = len(ranked) < 2 and not paths
    if starved:
        ranked = ranked + [s for s in _stems(text, 4) if s not in scores]

    # Dedupe paths but keep first-seen order: the first path in an issue is
    # nearly always the one the reporter meant.
    seen: set[str] = set()
    upaths = [p for p in paths if not (p in seen or seen.add(p))]
    return Probes(
        paths=upaths[:8],
        symbols=ranked[:max_symbols],
        anchors=anchors[:8],
        starved=starved,
    )


# --------------------------------------------------------------------------
# the search lane, driven through the real CLI


@dataclass
class Span:
    file: str
    start: int
    end: int
    weight: float
    origin: str

    @property
    def lines(self) -> int:
        return self.end - self.start + 1


class Lane:
    """Runs `ctx` verbs in a repo and collects addressed spans.

    Every call goes through the installed CLI, not an internal import: the
    thing under measurement is the surface an agent actually drives.
    """

    def __init__(self, root: Path, timeout: int = 180, verbose: bool = False):
        self.root = root
        self.timeout = timeout
        self.verbose = verbose
        self.calls = 0
        self.out_bytes = 0
        self.failures: list[str] = []

    def _ctx(self, *args: str) -> str:
        self.calls += 1
        try:
            r = subprocess.run(
                ["ctx", "--workspace", str(self.root), *args],
                capture_output=True, text=True, timeout=self.timeout,
                cwd=str(self.root),
                env={**os.environ, "CTX_NO_TELEMETRY": "1"},
            )
        except subprocess.TimeoutExpired:
            self.failures.append(f"timeout: ctx {' '.join(args[:2])}")
            return ""
        out = (r.stdout or "") + (r.stderr or "")
        self.out_bytes += len(out.encode())
        if self.verbose:
            print(f"    ctx {' '.join(args)} -> {len(out)}B rc={r.returncode}")
        return out

    # -- verbs ----------------------------------------------------------

    def search(self, patterns: list[str], max_matches: int = 40) -> list[Span]:
        if not patterns:
            return []
        out = self._ctx("search", "repo:.", *patterns, "--max-matches", str(max_matches))
        spans, cur = [], None
        for line in out.splitlines():
            if line.startswith(("[ctx", "patterns:", "coverage:", "result:", "next:", "snapshots:")):
                cur = None
                continue
            m = _SEARCH_FILE.match(line)
            if m and not line.startswith(" "):
                cand = m.group(1).strip()
                cur = cand if _looks_like_path(cand) else None
                continue
            h = _SEARCH_HIT.match(line)
            if h and cur:
                n = int(h.group(1))
                spans.append(Span(cur, n, n, 1.0, "search"))
        return spans

    def define(self, path: str, symbol: str) -> Span | None:
        out = self._ctx("def", f"repo:{path}:{symbol}")
        m = _DEF_SPAN.search(out)
        if not m:
            return None
        return Span(m.group(1), int(m.group(2)), int(m.group(3)), 3.0, "def")

    def refs(self, symbol: str, max_sites: int = 30) -> list[Span]:
        out = self._ctx("refs", symbol)
        spans = []
        for line in out.splitlines()[:max_sites]:
            m = _REFS_HIT.match(line)
            if m:
                n = int(m.group(2))
                spans.append(Span(m.group(1), n, n, 1.5, "refs"))
        return spans

    def graph(self, symbol: str, direction: str, max_sites: int = 20) -> list[Span]:
        """`ctx callers` / `ctx callees` — the neighbours of a symbol.

        This is the verb that reaches dispersed gold. When an issue names
        only the reporter's own application symbols and one framework path
        (a common shape in this corpus), search finds nothing and the
        traceback anchor finds one file; the other gold files are one call
        edge away and nothing else in the lane can reach them.
        """
        out = self._ctx(direction, symbol)
        spans = []
        for line in out.splitlines()[:max_sites]:
            m = _GRAPH_SITE.search(line)
            if m:
                spans.append(Span(m.group(1), int(m.group(2)), int(m.group(2)), 2.0, direction))
        return spans

    def repo_map(self, budget: int = 2000) -> list[str]:
        """The ranked file frame. Used as a tie-break, never as a retrieval claim."""
        out = self._ctx("map", "--budget", str(budget))
        return [
            m.group(1)
            for m in re.finditer(r"^repo:(\S+) · ", out, re.M)
        ]


def _looks_like_path(s: str) -> bool:
    return bool(re.match(rf"^[\w./\-]+\.(?:{_EXT_RE})$", s))


def _resolve_repo_path(root: Path, raw: str) -> str | None:
    """Bind a path named in an issue to a file in the tree, or give up.

    Issue reporters paste paths from their own machine:
    ``venv/lib/python3.6/site-packages/django/db/utils.py`` is the vendored
    copy of the very repository under test, and the useful part is the tail.
    Strip the vendor prefix, then fall back to a unique suffix match.
    """
    cand = raw.replace("\\", "/").lstrip("/")
    for c in (cand, _VENDOR.sub("", cand)):
        if c and (root / c).is_file():
            return c
    tail = cand.rsplit("/", 1)[-1]
    if not tail or "." not in tail:
        return None
    matches = [
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob(tail)
        if p.is_file() and ".git/" not in str(p)
    ]
    suffix = _VENDOR.sub("", cand)
    narrowed = [m for m in matches if m.endswith(suffix)]
    if len(narrowed) == 1:
        return narrowed[0]
    return matches[0] if len(matches) == 1 else None


_DECL = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:@\w|export\s+|public\s+|private\s+|protected\s+|static\s+|final\s+|"
    r"async\s+|pub(?:\([^)]*\))?\s+|unsafe\s+|inline\s+|const\s+|template\s*<)*"
    r"(?:def|class|func|fn|function|struct|interface|impl|trait|enum|type)\b"
    r"[\s(]*(?P<name>[A-Za-z_]\w*)?"
)
# C and friends declare without a keyword: `static int foo(void) {`.
_CDECL = re.compile(
    r"^(?P<indent>)(?:[A-Za-z_][\w*\s]{0,80}?)\b(?P<name>[A-Za-z_]\w*)\s*\([^;]*$"
)
_MAX_BLOCK = 400


def _enclosing_block(root: Path, path: str, line: int) -> tuple[int, int] | None:
    """The definition that contains `line`, found without a parser.

    ContextBench annotates gold as definition blocks, and a single grep hit
    can never cover one — that is why an earlier revision of this policy
    scored block recall 0.00 across the board while finding the right files.
    Retrieval has to hand back the enclosing definition, which is what an
    agent reads anyway.

    `ctx def` is the authoritative span and is used wherever the policy can
    name a symbol (origin `def`). This is the stand-in for the rest: one
    subprocess per grep hit would dominate the runtime. It walks up to the
    nearest declaration, then closes the block by dedent (indent-structured
    languages) or by brace balance (C-like). Spans it produces are labelled
    origin `block` in the record so the two are never conflated.
    """
    src = _file_lines(root, path)
    if not src or line > len(src):
        return None
    start = None
    for i in range(min(line, len(src)) - 1, max(-1, line - 1 - 200), -1):
        text = src[i]
        if not text.strip() or text.lstrip().startswith(("#", "//", "*")):
            continue
        m = _DECL.match(text) or _CDECL.match(text)
        if m:
            start = i
            indent = len(m.group("indent") or "")
            break
    if start is None:
        return None

    body = src[start]
    if "{" in body or body.rstrip().endswith((")", ",")) and "{" in "".join(
        src[start : start + 3]
    ):
        depth, end = 0, None
        for i in range(start, min(len(src), start + _MAX_BLOCK)):
            depth += src[i].count("{") - src[i].count("}")
            if depth <= 0 and i > start and "{" in "".join(src[start : i + 1]):
                end = i
                break
        if end is not None:
            return start + 1, end + 1

    end = start
    for i in range(start + 1, min(len(src), start + _MAX_BLOCK)):
        text = src[i]
        if not text.strip():
            continue
        if len(text) - len(text.lstrip()) <= indent:
            break
        end = i
    return start + 1, end + 1


def _symbols_at(root: Path, path: str, lines: list[int]) -> list[str]:
    """Nearest enclosing definition name above each hit line.

    A cheap, language-agnostic reader: find the last `def`/`class`/`func`/
    `fn`/`function`/type declaration at or above the hit. It is a heuristic
    to *feed ctx def*, not a parser — ctx def is what supplies the real span,
    and a wrong guess simply returns nothing.
    """
    try:
        src = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    decl = re.compile(
        r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|pub\s+)*"
        r"(?:def|class|func|fn|function|struct|interface|type|impl)\s+([A-Za-z_]\w*)"
    )
    names: list[str] = []
    for ln in lines:
        for i in range(min(ln, len(src)) - 1, -1, -1):
            m = decl.match(src[i])
            if m:
                if m.group(1) not in names:
                    names.append(m.group(1))
                break
    return names


def retrieve(
    lane: Lane, probes: Probes, budget_tokens: int, per_file_frac: float = 0.35
) -> tuple[list[Span], dict]:
    """The deterministic search-lane policy.

    Four stages, each one a verb an agent would run:
      1. anchor  — paths and traceback frames named in the issue text
      2. locate  — `ctx search` over the ranked symbol probes, plus
                   `ctx refs` on the strongest one
      3. expand  — `ctx def` for the definitions the issue actually named
      4. graph   — `ctx callers`/`ctx callees` from the anchors and those
                   definitions, which is the only way to reach gold that
                   sits one call edge away from anything the text named

    Then pack under the token budget. Precision is a budget property: an
    unbounded union would score high recall and be useless.
    """
    spans: list[Span] = []
    stages: dict[str, int] = {}

    # 1. anchors named outright in the issue. A whole small file is a
    #    legitimate anchor; a whole 3,000-line file is a precision disaster,
    #    so a large one is left to search to locate within.
    # (file, line) pairs the issue pointed at, kept for the graph stage: a
    # traceback frame names the exact function to expand from, and guessing
    # a line in the middle of the file instead throws that away.
    anchor_points: list[tuple[str, int]] = []
    for p in probes.paths:
        rp = _resolve_repo_path(lane.root, p)
        if rp:
            n = _line_count(lane.root / rp)
            anchor_points.append((rp, max(1, n // 2)))
            spans.append(
                Span(rp, 1, n, 2.0, "anchor") if n <= 400 else Span(rp, 1, 1, 0.1, "anchor")
            )
    for p, ln in probes.anchors:
        rp = _resolve_repo_path(lane.root, p)
        if rp:
            anchor_points.insert(0, (rp, ln))  # a real line beats a guessed one
            blk = _enclosing_block(lane.root, rp, ln)
            spans.append(
                Span(rp, *blk, 3.0, "block")
                if blk
                else Span(rp, max(1, ln - 20), ln + 20, 2.5, "anchor")
            )
    stages["anchor"] = len(spans)

    # 2. locate. One probe per call, not a batch: attribution is the point.
    #    A file hit by the issue's own class name must outrank a file hit by
    #    a generic word, and a batched call cannot tell which probe matched.
    hits: list[Span] = []
    top = probes.symbols[:8]
    for rank, sym in enumerate(top):
        w = 1.0 + (len(top) - rank) / len(top)  # 2.0 for the best probe, 1.125 for the worst
        for s in lane.search([sym], max_matches=25):
            hits.append(Span(s.file, s.start, s.end, w, "search"))
    stages["search_hits"] = len(hits)

    if probes.symbols:
        refs = lane.refs(probes.symbols[0])
        stages["refs"] = len(refs)
        hits.extend(refs)

    # 3. expand. Every hit becomes the definition that contains it — gold is
    #    annotated as definition blocks, and a bare hit line cannot cover
    #    one. `ctx def` supplies the authoritative span wherever a probe
    #    names the enclosing symbol; the rest fall back to `_enclosing_block`.
    by_file: dict[str, list[int]] = {}
    for h in hits:
        by_file.setdefault(h.file, []).append(h.start)

    probe_lc = {s.lower() for s in probes.symbols} | {
        part.lower() for s in probes.symbols for part in s.split(".")
    }
    ranked_files = sorted(by_file, key=lambda f: (-len(by_file[f]), f))[:8]
    defs = 0
    def_syms: list[str] = []
    for f in ranked_files:
        if defs >= 12:
            break
        for sym in _symbols_at(lane.root, f, sorted(set(by_file[f]))[:6]):
            # Only ask ctx def for symbols the issue actually named. Asking
            # for every symbol near a grep hit is what dragged an earlier
            # revision onto django's admin decorators.
            if sym.lower() not in probe_lc and not any(
                sym.lower() in p or p in sym.lower() for p in probe_lc if len(p) > 4
            ):
                continue
            s = lane.define(f, sym)
            if s:
                spans.append(s)
                def_syms.append(sym)
                defs += 1
            if defs >= 12:
                break
    stages["def_blocks"] = defs

    # 3b. graph expansion. Measured motivation: on a django instance whose
    #     issue named only the reporter's own model classes plus one
    #     traceback path, search returned zero hits and four of five gold
    #     files sat one call edge from the anchor. Search and refs cannot
    #     cross that edge; `ctx callers`/`ctx callees` is the verb that can.
    graph_hits: list[Span] = []
    seeds: list[str] = []
    for f, ln in anchor_points[:3]:
        seeds.extend(_symbols_at(lane.root, f, [ln]))
    seeds.extend(def_syms)
    for sym in [s for s in dict.fromkeys(filter(None, seeds))][:3]:
        for direction in ("callees", "callers"):
            graph_hits.extend(lane.graph(sym, direction))
    stages["graph"] = len(graph_hits)
    hits.extend(graph_hits)

    covered = {(s.file, s.start, s.end) for s in spans}
    blocks = 0
    for h in hits:
        blk = _enclosing_block(lane.root, h.file, h.start)
        if blk and (h.file, *blk) not in covered:
            spans.append(Span(h.file, blk[0], blk[1], h.weight, "block"))
            blocks += 1
        elif not blk:
            spans.append(h)  # unparseable region: the bare hit is all we have
    stages["expanded_blocks"] = blocks

    packed = _pack(lane.root, spans, budget_tokens, per_file_frac)
    stages["packed"] = len(packed)
    return packed, stages


def _line_count(p: Path) -> int:
    try:
        return sum(1 for _ in p.open("rb"))
    except OSError:
        return 1


def _pack(root: Path, spans: list[Span], budget_tokens: int,
          per_file_frac: float = 0.35) -> list[Span]:
    """Merge overlapping spans per file, then admit file-first under budget.

    Weight is summed across merged spans: a region three verbs agree on
    outranks one a single grep touched. Admission order is the only ranking
    decision in the policy, and it is the one a future change would tune.
    """
    per_file: dict[str, list[Span]] = {}
    for s in spans:
        per_file.setdefault(s.file, []).append(s)

    merged: list[Span] = []
    for f, group in per_file.items():
        group.sort(key=lambda s: (s.start, s.end))
        cur = None
        for s in group:
            if cur and s.start <= cur.end + 3:  # touching regions are one region
                cur = Span(f, cur.start, max(cur.end, s.end), cur.weight + s.weight,
                           cur.origin if cur.origin == s.origin else "mixed")
            else:
                if cur:
                    merged.append(cur)
                cur = Span(f, s.start, s.end, s.weight, s.origin)
        if cur:
            merged.append(cur)

    # Admission is file-first, then block. Interleaving blocks from every
    # file by density spends the budget one function at a time across twenty
    # files and tanks precision; gold context is concentrated, so the budget
    # should be too. A file's score is the evidence that landed in it, with a
    # bonus for agreement between sources: each probe carries a distinct
    # weight, so counting distinct weights per file approximates how many
    # different things pointed at it. Approximates, not measures — the graph
    # and anchor weights can collide with a probe's — which is fine for a
    # tie-break and would not be for a claim.
    file_score: dict[str, float] = {}
    file_probes: dict[str, set[float]] = {}
    for s in merged:
        file_score[s.file] = file_score.get(s.file, 0.0) + s.weight
        file_probes.setdefault(s.file, set()).add(round(s.weight, 3))
    for f in file_score:
        file_score[f] *= 1.0 + 0.5 * (len(file_probes[f]) - 1)

    ranked_files = sorted(file_score, key=lambda f: (-file_score[f], f))
    groups: dict[str, list[Span]] = {}
    for f in ranked_files:
        # Density within a file: a tight definition several verbs pointed at
        # beats a long one grazed once.
        groups[f] = sorted(
            (s for s in merged if s.file == f),
            key=lambda s: (-(s.weight / max(1, s.lines) ** 0.5), s.start),
        )

    # Two passes. The first caps what any single file may spend, so a
    # dispersed gold set is reachable at all — without it, django's
    # `lookups.py` absorbs a 32k budget alone and four of five gold files
    # are never seen. The second lifts the cap and tops up in file order, so
    # a genuinely single-file task still gets full coverage.
    cap = max(1200, int(budget_tokens * per_file_frac))
    # Pass one is deliberately narrow. Capping every file in a twenty-file
    # candidate set sprays the budget and leaves the top-ranked file with a
    # third of the block coverage it had uncapped; capping only the handful
    # that could plausibly be the dispersed gold set costs nothing.
    breadth = max(3, int(round(1 / max(per_file_frac, 0.05))))
    out: list[Span] = []
    taken: set[tuple[str, int, int]] = set()
    spent = 0
    for limit, scope in ((cap, ranked_files[:breadth]), (budget_tokens, ranked_files)):
        for f in scope:
            in_file = 0
            for s in groups[f]:
                key = (s.file, s.start, s.end)
                if key in taken:
                    continue
                cost = _span_tokens(root, s)
                if in_file + cost > limit or spent + cost > budget_tokens:
                    continue
                out.append(s)
                taken.add(key)
                in_file += cost
                spent += cost
            if spent >= budget_tokens:
                break
        if spent >= budget_tokens:
            break
    return out


_FILE_CACHE: dict[str, list[str]] = {}


def _file_lines(root: Path, rel: str) -> list[str]:
    key = f"{root}::{rel}"
    if key not in _FILE_CACHE:
        try:
            _FILE_CACHE[key] = (root / rel).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            _FILE_CACHE[key] = []
    return _FILE_CACHE[key]


def _span_tokens(root: Path, s: Span) -> int:
    src = _file_lines(root, s.file)
    if not src:
        return s.lines * 8  # unreadable: charge a pessimistic constant
    body = "\n".join(src[s.start - 1 : s.end])
    return max(1, len(body.encode()) // BYTES_PER_TOKEN)


# --------------------------------------------------------------------------
# scoring: file / block / line, per the paper's definitions


def score(spans: list[Span], gold: list[dict], block_overlap: float) -> dict:
    """File / block / line scoring, delegated to :mod:`ctx.trajectory`.

    The metric has one definition and it lives in ``src``, because the same
    numbers are computed there for agent trajectories (``ctx replay --gold``).
    Two copies would drift the moment one of them was tuned, and a benchmark
    whose metric quietly differs between arms measures nothing.
    """
    from ctx.trajectory import Region, score_regions

    regions = [Region(sp.file, sp.start, sp.end, sp.origin, "parsed") for sp in spans]
    scored = score_regions(regions, gold, block_overlap=block_overlap)
    c = scored["counts"]
    # This runner's older field names, kept so recorded receipts stay readable.
    scored["counts"] = {
        "gold_files": c["gold_files"],
        "gold_blocks": c["gold_blocks"],
        "gold_lines": c["gold_lines"],
        "retrieved_files": c["observed_files"],
        "retrieved_spans": c["observed_regions"],
        "retrieved_lines": c["observed_lines"],
    }
    return scored


def _mean(xs: list[float]) -> float:
    return round(statistics.fmean(xs), 4) if xs else 0.0


# --------------------------------------------------------------------------
# runner


def run_instance(
    inst: dict, repos: Path, budgets: list[int], block_overlap: float, verbose: bool
) -> dict | None:
    gold = gold_blocks(inst)
    if not gold:
        return None
    root = materialize(inst, repos)
    if root is None:
        return None

    probes = extract_probes(inst.get("problem_statement", ""))
    lane = Lane(root, verbose=verbose)

    # Gold files that do not exist at base_commit are annotation drift, not
    # a retrieval miss. Report them; never charge them to the lane.
    gold, missing = resolve_gold(gold, root)
    if not gold:
        print(f"  all {len(missing)} gold paths unresolvable at base_commit — not scored")
        return None

    arms = {}
    for b in budgets:
        spans, stages = retrieve(lane, probes, b)
        s = score(spans, gold, block_overlap)
        s["stages"] = stages
        s["retrieved_tokens"] = sum(_span_tokens(root, sp) for sp in spans)
        # Evidence density, per BENCHMARK.md: gold lines delivered per token
        # the model would have had to read.
        gl = sum(
            len(
                set(range(bb["start"], bb["end"] + 1))
                & {
                    n
                    for sp in spans
                    if sp.file == bb["file"]
                    for n in range(sp.start, sp.end + 1)
                }
            )
            for bb in gold
        )
        s["evidence_density"] = round(gl / max(1, s["retrieved_tokens"]), 5)
        arms[str(b)] = s

    return {
        "instance_id": inst["instance_id"],
        "original_inst_id": inst.get("original_inst_id"),
        "repo": inst["repo"],
        "language": inst["language"],
        "source": inst.get("source"),
        "dispersion": _dispersion(gold),
        "probes": {
            "paths": probes.paths,
            "symbols": probes.symbols,
            "starved": probes.starved,
        },
        "gold_files_missing_at_base": missing,
        "ctx_calls": lane.calls,
        "ctx_output_tokens": lane.out_bytes // BYTES_PER_TOKEN,
        "lane_failures": lane.failures,
        "arms": arms,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--config", choices=sorted(CONFIGS), default="verified")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--language", default="", help="comma-separated filter")
    ap.add_argument("--source", default="", help="Verified,Pro,Poly,Multi")
    ap.add_argument("--repo", default="", help="comma-separated owner/name filter")
    ap.add_argument("--instance", default="", help="one instance_id, ignores filters")
    ap.add_argument(
        "--stratify", action="store_true",
        help="balance the pick over language x edit-dispersion instead of taking the head",
    )
    ap.add_argument(
        "--budgets", default="2000,8000,32000",
        help="comma-separated retrieved-token budgets; one arm each",
    )
    ap.add_argument("--block-overlap", type=float, default=0.5)
    ap.add_argument(
        "--emit-gold", default="",
        help="write ctx.gold/v1 files for the selected instances into this "
             "directory instead of running retrieval; feed them to "
             "`ctx replay --gold` to score a real agent trajectory",
    )
    ap.add_argument("--json", default="", help="write the full record here")
    ap.add_argument("--keep", action="store_true", help="keep cloned trees")
    ap.add_argument("--refresh", action="store_true", help="re-fetch the corpus cache")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    work = Path(args.workdir).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    repos = work / "repos"
    repos.mkdir(exist_ok=True)
    budgets = [int(b) for b in args.budgets.split(",") if b.strip()]

    rows = load_corpus(work, args.config, args.refresh)
    if args.instance:
        rows = [r for r in rows if r["instance_id"] == args.instance]
    else:
        if args.language:
            keep = {x.strip() for x in args.language.split(",")}
            rows = [r for r in rows if r["language"] in keep]
        if args.source:
            keep = {x.strip() for x in args.source.split(",")}
            rows = [r for r in rows if r.get("source") in keep]
        if args.repo:
            keep = {x.strip() for x in args.repo.split(",")}
            rows = [r for r in rows if r["repo"] in keep]
        rows = (
            stratified(rows, args.limit)
            if args.stratify
            else sorted(rows, key=lambda r: r["instance_id"])[: args.limit]
        )

    if not rows:
        raise SystemExit("no instances matched the filters")

    if args.emit_gold:
        out = Path(args.emit_gold)
        out.mkdir(parents=True, exist_ok=True)
        written = 0
        for inst in rows:
            blocks = gold_blocks(inst)
            if not blocks:
                continue
            (out / f"{inst['instance_id']}.json").write_text(
                json.dumps(
                    {
                        "schema": "ctx.gold/v1",
                        "instance_id": inst["instance_id"],
                        "repo": inst["repo"],
                        "base_commit": inst["base_commit"],
                        "language": inst["language"],
                        "problem_statement": inst.get("problem_statement", ""),
                        # `root` is filled in per machine at scoring time; the
                        # annotations themselves are repo-relative.
                        "root": "",
                        "blocks": [
                            {"file": b["file"], "start": b["start"], "end": b["end"]}
                            for b in blocks
                        ],
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
            written += 1
        print(f"wrote {written} ctx.gold/v1 files to {out}")
        print("score a trajectory with: ctx replay --gold <file> <transcript.jsonl>")
        return

    print(
        f"ContextBench/{CONFIGS[args.config]} · {len(rows)} instances · "
        f"budgets {budgets} · block-overlap {args.block_overlap}"
    )
    print("policy: searchlane (deterministic, model-free) — teacher, not referee\n")

    records, skipped = [], 0
    for i, inst in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {inst['instance_id']} ({inst['language']})", flush=True)
        try:
            rec = run_instance(inst, repos, budgets, args.block_overlap, args.verbose)
        except Exception as e:  # noqa: BLE001 — a repo that will not clone is data
            print(f"  error: {type(e).__name__}: {str(e)[:160]}")
            skipped += 1
            continue
        if rec is None:
            skipped += 1
            continue
        records.append(rec)
        top = rec["arms"][str(budgets[-1])]
        print(
            f"  gold {top['counts']['gold_blocks']} blocks / "
            f"{top['counts']['gold_files']} files · "
            f"file F1 {top['file']['f1']:.2f} · block recall {top['block']['recall']:.2f} · "
            f"line recall {top['line']['recall']:.2f} · "
            f"{top['retrieved_tokens']:,} ret tok · {rec['ctx_calls']} ctx calls"
        )
        if rec["gold_files_missing_at_base"]:
            print(f"  annotation drift (not charged): {rec['gold_files_missing_at_base']}")
        if not args.keep:
            for d in repos.iterdir():
                if d.name.startswith(inst["repo"].replace("/", "__") + "@"):
                    shutil.rmtree(d, ignore_errors=True)

    if not records:
        raise SystemExit("no instances scored")

    starved = [r for r in records if r["probes"]["starved"]]
    print("\n== aggregate ==")
    print(f"{len(records)} scored, {skipped} unavailable")
    if starved:
        print(
            f"{len(starved)} probe-starved (issue text carries no path, no "
            f"traceback and fewer than {_PROSE_MIN} code-shaped identifiers — "
            f"a model-free policy has nothing to anchor on; scored anyway, "
            f"broken out below)"
        )
    print()
    hdr = f"{'budget':>8} {'fileF1':>7} {'blkR':>6} {'blkP':>6} {'lineR':>6} {'lineP':>6} {'retTok':>8} {'dens':>7}"
    print(hdr)
    print("-" * len(hdr))
    agg = {}
    for b in budgets:
        k = str(b)
        a = {
            "file_f1": _mean([r["arms"][k]["file"]["f1"] for r in records]),
            "file_recall": _mean([r["arms"][k]["file"]["recall"] for r in records]),
            "block_recall": _mean([r["arms"][k]["block"]["recall"] for r in records]),
            "block_precision": _mean([r["arms"][k]["block"]["precision"] for r in records]),
            "line_recall": _mean([r["arms"][k]["line"]["recall"] for r in records]),
            "line_precision": _mean([r["arms"][k]["line"]["precision"] for r in records]),
            "retrieved_tokens": _mean([r["arms"][k]["retrieved_tokens"] for r in records]),
            "evidence_density": _mean([r["arms"][k]["evidence_density"] for r in records]),
        }
        agg[k] = a
        print(
            f"{b:>8} {a['file_f1']:>7.3f} {a['block_recall']:>6.3f} "
            f"{a['block_precision']:>6.3f} {a['line_recall']:>6.3f} "
            f"{a['line_precision']:>6.3f} {a['retrieved_tokens']:>8,.0f} "
            f"{a['evidence_density']:>7.4f}"
        )

    by_lang: dict[str, list[dict]] = {}
    by_disp: dict[str, list[dict]] = {}
    by_probe: dict[str, list[dict]] = {}
    for r in records:
        by_lang.setdefault(r["language"], []).append(r)
        by_disp.setdefault(r["dispersion"], []).append(r)
        by_probe.setdefault(
            "probe-starved" if r["probes"]["starved"] else "probe-anchored", []
        ).append(r)
    top = str(budgets[-1])
    for title, grouping in (
        ("probe availability", by_probe),
        ("language", by_lang),
        ("dispersion", by_disp),
    ):
        print(f"\nby {title} (budget {top}):")
        for k in sorted(grouping):
            g = grouping[k]
            print(
                f"  {k:<14} n={len(g):<3} file F1 "
                f"{_mean([x['arms'][top]['file']['f1'] for x in g]):.3f} · block recall "
                f"{_mean([x['arms'][top]['block']['recall'] for x in g]):.3f} · line recall "
                f"{_mean([x['arms'][top]['line']['recall'] for x in g]):.3f}"
            )

    # The defect queue: the point of the whole exercise. A gold file the lane
    # never named is a ranking or verb defect with an address.
    print("\n== defect queue (gold files the lane never named, top budget) ==")
    misses: list[tuple[str, str]] = []
    for r in records:
        ret = {b["file"] for b in r["arms"][top]["blocks"] if b["verdict"] != "missed"}
        gold_f = {b["file"] for b in r["arms"][top]["blocks"]}
        for f in sorted(gold_f - ret):
            if f not in r["gold_files_missing_at_base"]:
                misses.append((r["instance_id"], f))
    for iid, f in misses[:40]:
        print(f"  {iid}: {f}")
    if len(misses) > 40:
        print(f"  ... {len(misses) - 40} more (see --json)")
    if not misses:
        print("  empty")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "dataset": DATASET,
                    "config": CONFIGS[args.config],
                    "policy": "searchlane/deterministic",
                    "block_overlap": args.block_overlap,
                    "budgets": budgets,
                    "scored": len(records),
                    "unavailable": skipped,
                    "aggregate": agg,
                    "instances": records,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
