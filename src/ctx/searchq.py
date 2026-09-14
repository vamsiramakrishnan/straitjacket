"""The search query language: Sourcegraph-shaped filters in the pattern
list, and git history as a first-class evidence source.

    ctx search repo: TokenBucket file:src/ -file:tests lang:python
    ctx search repo: "rate limit" case:no
    ctx search repo: sym:resolve_refs               # where is it defined
    ctx search repo: type:commit "prefix tax"       # commits whose message says so
    ctx search repo: type:diff ENABLE_TOOL_SEARCH   # commits whose diff adds/removes it
    ctx search repo: type:diff foo after:2026-08-01 author:vamsi

Filters are tokens of the form ``key:value``; everything else is a pattern.
The same grammar serves ``ctx search``, the ``q`` ``search`` stage and
``ctx pack``, so a filter learned once works everywhere.

History is evidence, not decoration: a commit row carries its hash, date,
author, subject and the files it touched, in the same bounded, deterministic
shape as a search hit — the model can cite it, and ``ctx get`` can open the
files it names. Nothing here invents a database: git already keeps the
history index (``git log -S/-G/--grep``), and the shell-out is bounded by
``--max-count`` and a timeout.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from ctx.workspace import Workspace

FILTER_KEYS = (
    "file", "-file", "lang", "-lang", "case", "sym", "type", "after", "before",
    "author", "rev", "path", "-path",
)

_LANG_ALIASES = {
    "py": "python", "python": "python",
    "ts": "typescript", "typescript": "typescript", "tsx": "typescript",
    "js": "javascript", "javascript": "javascript", "jsx": "javascript",
    "go": "go", "golang": "go", "rs": "rust", "rust": "rust",
    "java": "java", "kt": "kotlin", "kotlin": "kotlin", "rb": "ruby", "ruby": "ruby",
    "php": "php", "c": "c", "cpp": "c++", "c++": "c++", "cxx": "c++", "cc": "c++",
    "cs": "c#", "c#": "c#", "csharp": "c#", "swift": "swift", "scala": "scala",
    "lua": "lua", "sh": "shell", "bash": "shell", "shell": "shell",
}

#: Commit rows returned per history search unless the caller narrows it.
HISTORY_MAX = 50
HISTORY_TIMEOUT_S = 20


@dataclass
class Query:
    patterns: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)       # file:/path: includes
    not_files: list[str] = field(default_factory=list)   # -file:/-path: excludes
    langs: set[str] = field(default_factory=set)
    not_langs: set[str] = field(default_factory=set)
    case: bool | None = None                             # None = engine default
    syms: list[str] = field(default_factory=list)
    kind: str = "code"                                   # code | commit | diff
    after: str | None = None
    before: str | None = None
    author: str | None = None
    rev: str | None = None
    unknown: list[str] = field(default_factory=list)     # key:value with unknown key

    @property
    def has_filters(self) -> bool:
        return bool(
            self.files or self.not_files or self.langs or self.not_langs
            or self.case is not None or self.syms or self.kind != "code"
            or self.after or self.before or self.author or self.rev
        )

    def describe(self) -> str:
        """The filters, in a stable one-line spelling for headers."""
        parts: list[str] = []
        parts += [f"file:{f}" for f in self.files]
        parts += [f"-file:{f}" for f in self.not_files]
        parts += [f"lang:{lang}" for lang in sorted(self.langs)]
        parts += [f"-lang:{lang}" for lang in sorted(self.not_langs)]
        if self.case is not None:
            parts.append("case:yes" if self.case else "case:no")
        parts += [f"sym:{s}" for s in self.syms]
        if self.kind != "code":
            parts.append(f"type:{self.kind}")
        for key in ("after", "before", "author", "rev"):
            val = getattr(self, key)
            if val:
                parts.append(f"{key}:{val}")
        return " ".join(parts)


class QueryError(ValueError):
    pass


_FILTER_RE = re.compile(r"^([-!]?[a-z]+):(.+)$", re.DOTALL)


def parse(tokens: list[str]) -> Query:
    """Split ``tokens`` into patterns and filters. A token is a filter only
    when its key is known — ``re:foo`` stays a pattern, and ``file:`` with an
    empty value is an error rather than a silent no-op."""
    q = Query()
    for tok in tokens:
        m = _FILTER_RE.match(tok)
        # ``!file:`` is the shell-safe spelling of ``-file:`` (argparse eats a
        # leading dash unless the caller writes ``--`` first).
        key = m.group(1).replace("!", "-") if m else ""
        if not m or key not in FILTER_KEYS:
            q.patterns.append(tok)  # ``re:foo`` is a pattern, not a filter
            continue
        val = m.group(2)
        if key in ("file", "path"):
            q.files.append(val)
        elif key in ("-file", "-path"):
            q.not_files.append(val)
        elif key in ("lang", "-lang"):
            lang = _LANG_ALIASES.get(val.lower())
            if lang is None:
                raise QueryError(
                    f"unknown language {val!r}; known: "
                    + ", ".join(sorted(set(_LANG_ALIASES.values())))
                )
            (q.langs if key == "lang" else q.not_langs).add(lang)
        elif key == "case":
            v = val.lower()
            if v in ("yes", "y", "true", "1"):
                q.case = True
            elif v in ("no", "n", "false", "0"):
                q.case = False
            else:
                raise QueryError("case: takes yes or no")
        elif key == "sym":
            q.syms.append(val)
        elif key == "type":
            v = val.lower()
            if v not in ("code", "commit", "diff"):
                raise QueryError("type: takes code, commit or diff")
            q.kind = v
        elif key == "after":
            q.after = val
        elif key == "before":
            q.before = val
        elif key == "author":
            q.author = val
        elif key == "rev":
            q.rev = val
    return q


# ------------------------------------------------------------- path filters
def _path_matches(rel: str, spec: str) -> bool:
    """``file:`` semantics: a glob when it looks like one, else a regex
    searched anywhere in the path (Sourcegraph's ``file:`` is a regex)."""
    if any(ch in spec for ch in "*?["):
        return fnmatch.fnmatch(rel, spec) or fnmatch.fnmatch(rel, f"*{spec}") or fnmatch.fnmatch(rel, f"{spec}*")
    try:
        return re.search(spec, rel) is not None
    except re.error:
        return spec in rel


def keep_path(rel: str, q: Query, lang_of=None) -> bool:
    """Whether ``rel`` survives the query's path and language filters."""
    if q.files and not any(_path_matches(rel, f) for f in q.files):
        return False
    if q.not_files and any(_path_matches(rel, f) for f in q.not_files):
        return False
    if q.langs or q.not_langs:
        if lang_of is None:
            from ctx.skeleton import language_for as lang_of  # type: ignore[assignment]
        lang = lang_of(rel)
        if q.langs and lang not in q.langs:
            return False
        if q.not_langs and lang in q.not_langs:
            return False
    return True


# ------------------------------------------------------------------ history
@dataclass(frozen=True, slots=True)
class CommitRow:
    sha: str
    date: str
    author: str
    subject: str
    files: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "sha": self.sha, "date": self.date, "author": self.author,
            "subject": self.subject, "files": list(self.files),
        }


def _git(ws: Workspace, argv: list[str], timeout: float = HISTORY_TIMEOUT_S) -> str:
    proc = subprocess.run(
        ["git", *argv], cwd=str(ws.root), capture_output=True, text=True,
        timeout=timeout, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        raise QueryError(tail[-1] if tail else "git failed")
    return proc.stdout


def history(ws: Workspace, q: Query, *, max_count: int = HISTORY_MAX) -> list[CommitRow]:
    """Commits matching the query: ``type:commit`` searches messages
    (``--grep``, every pattern must match), ``type:diff`` searches what
    changed (``-G`` regex, or ``-S`` for a fixed string). Path filters
    restrict the history to those paths; ``after:``/``before:``/``author:``/
    ``rev:`` map onto git's own options. Newest first, bounded."""
    if ws.git is None:
        raise QueryError("history search needs a git workspace")
    argv = ["log", f"--max-count={int(max_count)}", "--date=short",
            "--format=%x1e%h%x1f%ad%x1f%an%x1f%s", "--name-only", "--no-merges"]
    if q.kind == "commit":
        for p in q.patterns:
            argv += ["--grep", p]
        argv.append("--regexp-ignore-case" if q.case is False else "--extended-regexp")
    elif q.kind == "diff":
        if not q.patterns:
            raise QueryError("type:diff needs a pattern to look for in the changes")
        for p in q.patterns:
            argv += ["-G", p]
        if q.case is False:
            argv.append("--regexp-ignore-case")
    else:
        raise QueryError("history search needs type:commit or type:diff")
    if q.after:
        argv.append(f"--since={q.after}")
    if q.before:
        argv.append(f"--until={q.before}")
    if q.author:
        argv += ["--author", q.author]
    if q.rev:
        argv.append(q.rev)
    paths = [f for f in q.files if not any(ch in f for ch in "*?[")]
    if paths or q.langs:
        argv.append("--")
        argv += paths
        if q.langs:
            from ctx.skeleton import _LANG_BY_EXT

            argv += [f"*{ext}" for ext, lang in _LANG_BY_EXT.items() if lang in q.langs]
    out = _git(ws, argv)
    rows: list[CommitRow] = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, files = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        touched = tuple(sorted(f for f in files.splitlines() if f.strip()))
        if q.not_files:
            touched = tuple(f for f in touched if not any(_path_matches(f, x) for x in q.not_files))
        rows.append(CommitRow(parts[0], parts[1], parts[2], parts[3], touched))
    return rows


def last_change(ws: Workspace, rel: str, *, line: int | None = None) -> CommitRow | None:
    """The commit that last touched ``rel`` (or its line ``line``): the
    ``history`` stage's per-site evidence. None outside git or for an
    uncommitted file."""
    if ws.git is None:
        return None
    try:
        if line is not None:
            out = _git(ws, ["blame", "-L", f"{line},{line}", "--porcelain", "--", rel], timeout=10)
            sha = out.split(" ", 1)[0][:12]
            if not sha or set(sha) == {"0"}:
                return None
            show = _git(ws, ["show", "-s", "--date=short", "--format=%h%x1f%ad%x1f%an%x1f%s", sha], timeout=10)
        else:
            show = _git(ws, ["log", "-1", "--date=short", "--format=%h%x1f%ad%x1f%an%x1f%s", "--", rel], timeout=10)
    except (QueryError, subprocess.SubprocessError, OSError):
        return None
    parts = show.strip().split("\x1f")
    if len(parts) < 4:
        return None
    return CommitRow(parts[0], parts[1], parts[2], parts[3], (rel,))


def render_commit(row: CommitRow, *, max_files: int = 6) -> str:
    files = ", ".join(row.files[:max_files])
    more = len(row.files) - max_files
    if more > 0:
        files += f" (+{more})"
    return f"{row.sha} {row.date} {row.author} · {row.subject}" + (f"\n    files: {files}" if files else "")


def render_history(ref_display: str, q: Query, rows: list[CommitRow], *, cap: int, blob: str) -> str:
    """The `ctx search … type:commit|diff` rendering: header, one commit per
    row with the files it touched, a coverage line, the result handle and
    the next commands that open the evidence."""
    out: list[str] = [f"[ctx search {ref_display} · history]"]
    out.append("patterns: " + " ".join(repr(p) for p in q.patterns) + f" · {q.describe()}")
    for row in rows:
        out.append("  " + render_commit(row))
    out.append("coverage:")
    out.append(
        f"  commits: {len(rows)} (newest first, bounded at {cap})"
        + (" · at the bound; narrow with after:/file:/author:" if len(rows) >= cap else "")
    )
    out.append("result: blob:" + blob)
    if rows:
        out.append("next:")
        out.append(f"  ctx run -- git show --stat {rows[0].sha}   # the change itself")
        out.append(f"  ctx get repo:{rows[0].files[0]}" if rows[0].files else "  ctx get repo:<file>")
    return "\n".join(out)
