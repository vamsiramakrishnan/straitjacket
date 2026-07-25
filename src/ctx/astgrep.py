"""ast-grep engine tier (docs/EVIDENCE-PLANS.md P2).

ast-grep is an opportunistic binary (the ripgrep pattern): on PATH it gives
structural, metavariable-pattern search and mechanical rewrites over
tree-sitter ASTs. ``ast.search`` degrades through three rungs, each honestly
disclosed: the ast-grep BINARY (structural) → the ``ast-grep-py`` LIBRARY
(structural, same tree-sitter engine, no subprocess) → a metavariable-anchored
regex scan with its precision labeled ``textual``. ``ast.rewrite.*`` stays
binary-only and declines otherwise (a textual approximation of a codemod is
the failure mode, not a feature — no lossy fallback, and the library rung is
deliberately search-only).

Determinism: matches are sorted ``(path, line, column)``, paths are
repo-relative POSIX, the engine name + version are disclosed in node meta
and participate in node cache keys. The binary is probed by running
``--version`` and checking for "ast-grep" in the output — ``sg`` alone is
NOT trusted (shadow-utils ships an unrelated ``sg``).

Rewrites: ``rewrite_preview`` computes the full patch as a unified diff
against current file bytes and mints it as an addressable ``blob:``;
``rewrite_apply`` is transactional (``git apply`` of the previewed patch,
all-or-nothing) and generation-guarded — it refuses if the source-state
generation changed since preview.
"""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
from functools import lru_cache
from typing import Any

from ctx.sessiondir import LEDGER_DIR_NAME
from ctx.store import Store
from ctx.textutil import EVIDENCE_LINE_CHARS
from ctx.workspace import Workspace

_PROBE_TIMEOUT = 10.0
_RUN_TIMEOUT = 120.0


class EngineMissing(Exception):
    """The required engine is not installed (handled per on_missing)."""


class RewriteError(Exception):
    pass


# ------------------------------------------------------------------- probe
@lru_cache(maxsize=1)
def binary() -> tuple[str, str] | None:
    """(path, version) of a real ast-grep, else None. Probes ``ast-grep``
    then ``sg`` — the latter only counts if ``--version`` says ast-grep."""
    for name in ("ast-grep", "sg"):
        path = shutil.which(name)
        if not path:
            continue
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, timeout=_PROBE_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = (out.stdout or out.stderr).decode("utf-8", "replace").strip()
        if out.returncode == 0 and "ast-grep" in text.lower():
            version = text.split()[-1] if text.split() else "?"
            return path, version
    return None


def available() -> bool:
    return binary() is not None


@lru_cache(maxsize=1)
def lib_available() -> bool:
    """True when the ``ast-grep-py`` library is importable — the middle rung
    of ast.search (structural, in-process, no subprocess). Cached; call
    ``lib_available.cache_clear()`` in tests that manipulate imports."""
    try:
        import ast_grep_py  # noqa: F401
    except Exception:
        return False
    return True


def _lib_version() -> str:
    try:
        from importlib.metadata import version

        return version("ast_grep_py")
    except Exception:
        return "?"


def engine_id() -> str:
    """Engine identity for disclosure and cache keys. Precedence mirrors the
    ast.search dispatch: binary id > library id > regex fallback."""
    b = binary()
    if b is not None:
        return f"ast-grep {b[1]}"
    if lib_available():
        return f"ast-grep-py {_lib_version()}"
    return "regex-fallback"


# ast_grep_py language strings, keyed by the skeleton language name that
# ``ctx.skeleton.language_for`` derives from a repo-relative path. Names that
# differ from the skeleton spelling (c++/c#/shell) are remapped; a path whose
# language cannot be determined maps to None and is skipped per-file.
_AST_GREP_LANG = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "kotlin": "kotlin",
    "ruby": "ruby",
    "php": "php",
    "c": "c",
    "c++": "cpp",
    "c#": "csharp",
    "swift": "swift",
    "scala": "scala",
    "lua": "lua",
    "shell": "bash",
}


def _lib_lang(rel: str) -> str | None:
    """ast_grep_py language string for a repo-relative path, or None."""
    try:
        from ctx.skeleton import language_for

        sk = language_for(rel)
    except Exception:
        return None
    if not sk:
        return None
    return _AST_GREP_LANG.get(sk.lower())


# ------------------------------------------------------------------ search
def _ledger_path(rel: str) -> bool:
    return LEDGER_DIR_NAME in rel.replace("\\", "/").split("/")


def _parse_stream(raw: bytes) -> list[dict[str, Any]]:
    """Parse ``--json=stream`` output (one JSON match per line); tolerant of
    a single JSON array too (older versions)."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            docs = json.loads(text)
            return [d for d in docs if isinstance(d, dict)]
        except json.JSONDecodeError:
            return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def _match_row(doc: dict[str, Any]) -> dict[str, Any] | None:
    rel = str(doc.get("file") or "")
    if not rel:
        return None
    rng = doc.get("range") or {}
    start = rng.get("start") or {}
    line = int(start.get("line", 0)) + 1  # ast-grep lines are 0-based
    col = int(start.get("column", 0))
    text = str(doc.get("lines") or doc.get("text") or "").splitlines()
    first = text[0].strip()[:EVIDENCE_LINE_CHARS] if text else ""
    return {"file": rel.replace("\\", "/"), "line": line, "col": col, "text": first}


def _run_astgrep(ws: Workspace, argv_tail: list[str]) -> bytes:
    b = binary()
    if b is None:
        raise EngineMissing("ast-grep is not installed")
    argv = [b[0], *argv_tail]
    try:
        out = subprocess.run(
            argv, cwd=str(ws.root), capture_output=True, timeout=_RUN_TIMEOUT
        )
    except subprocess.TimeoutExpired as e:
        raise RewriteError(f"ast-grep timed out after {_RUN_TIMEOUT:.0f}s") from e
    if out.returncode not in (0, 1):  # 1 = no matches for some subcommands
        tail = out.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RewriteError(
            "ast-grep failed: " + (tail[-1] if tail else f"exit {out.returncode}")
        )
    return out.stdout


def _fallback_regex(pattern: str) -> str:
    """Metavariable-anchored regex derived from an ast-grep pattern: literal
    fragments are escaped, ``$$$X`` → lazy any-run, ``$X`` → lazy non-space
    run. Textual precision — labeled, never passed off as structural."""
    import re as _re

    parts: list[str] = []
    i = 0
    for m in _re.finditer(r"\$\$\$[A-Z_][A-Z0-9_]*|\$[A-Z_][A-Z0-9_]*", pattern):
        parts.append(_re.escape(pattern[i : m.start()]))
        parts.append(".*?" if m.group(0).startswith("$$$") else r"[^\s,()]+")
        i = m.end()
    parts.append(_re.escape(pattern[i:]))
    return "".join(parts)


def ast_search(
    ws: Workspace,
    store: Store,
    pattern: str,
    *,
    language: str | None = None,
    glob: str | None = None,
    cap: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Structural search. Returns (rows, meta); rows sorted (file, line,
    col), capped with the overflow declared by the caller. meta carries
    the engine disclosure and honest precision label."""
    b = binary()
    if b is not None:
        tail = ["run", "--pattern", pattern, "--json=stream"]
        if language:
            tail += ["--lang", language]
        if glob:
            tail += ["--globs", glob]
        tail.append(".")
        raw = _run_astgrep(ws, tail)
        rows = []
        for doc in _parse_stream(raw):
            row = _match_row(doc)
            if row is None or _ledger_path(row["file"]) or ws.is_ignored(row["file"]):
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r["file"], r["line"], r["col"]))
        meta = {"engine": engine_id(), "precision": "structural"}
        return rows[: max(1, cap)], {**meta, "matched": len(rows)}

    if lib_available():
        return _lib_search(ws, store, pattern, language, glob, cap)

    # Degraded tier: metavariable-anchored regex over repo targets, honestly
    # labeled. Reuses the retrieval target walk (path-sorted, confined).
    import re as _re

    from ctx.refs import parse_ref
    from ctx.retrieval import _resolve_repo_targets

    rx = _re.compile(_fallback_regex(pattern))
    targets, _, _ = _resolve_repo_targets(store, ws, parse_ref("repo:"), glob=glob, scope=None)
    rows = []
    for t in targets:
        rel = str(t.label)
        if _ledger_path(rel):
            continue
        if language and not _language_matches(rel, language):
            continue
        for i, ln in enumerate(t.text.splitlines(), start=1):
            if rx.search(ln):
                rows.append({"file": rel, "line": i, "col": 0, "text": ln.strip()[:EVIDENCE_LINE_CHARS]})
    rows.sort(key=lambda r: (r["file"], r["line"], r["col"]))
    meta = {
        "engine": "regex-fallback",
        "precision": "textual (metavariable-anchored regex; ast-grep absent)",
        "matched": len(rows),
    }
    return rows[: max(1, cap)], meta


def _lib_search(
    ws: Workspace,
    store: Store,
    pattern: str,
    language: str | None,
    glob: str | None,
    cap: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Middle rung: structural search via the ``ast-grep-py`` library over the
    same repo-target walk the regex fallback uses. Parses each file in-process
    with the tree-sitter engine; per-file parse/find is wrapped so a file whose
    language cannot be mapped (or that the lib rejects) is skipped silently.
    Rows share the binary path's shape and are sorted ``(file, line, col)``."""
    from ast_grep_py import SgRoot

    from ctx.refs import parse_ref
    from ctx.retrieval import _resolve_repo_targets

    targets, _, _ = _resolve_repo_targets(
        store, ws, parse_ref("repo:"), glob=glob, scope=None
    )
    rows: list[dict[str, Any]] = []
    for t in targets:
        rel = str(t.label)
        if _ledger_path(rel) or ws.is_ignored(rel):
            continue
        lang = _lib_lang(rel)
        if lang is None:
            continue
        if language and lang.lower() != language.lower():
            continue
        try:
            root = SgRoot(t.text, lang).root()
            matches = root.find_all(pattern=pattern)
        except Exception:
            continue  # unparseable / lib-rejected file: skip, do not error
        for node in matches:
            start = node.range().start
            line = int(start.line) + 1  # ast_grep_py ranges are 0-based lines
            col = int(start.column)
            text = node.text().splitlines()
            first = text[0].strip()[:EVIDENCE_LINE_CHARS] if text else ""
            rows.append({"file": rel, "line": line, "col": col, "text": first})
    rows.sort(key=lambda r: (r["file"], r["line"], r["col"]))
    meta = {"engine": engine_id(), "precision": "structural", "matched": len(rows)}
    return rows[: max(1, cap)], meta


def _language_matches(rel: str, language: str) -> bool:
    try:
        from ctx.skeleton import language_for

        return (language_for(rel) or "").lower() == language.lower()
    except Exception:
        return True  # fail-open: don't silently drop files on a helper error


# ----------------------------------------------------------------- rewrite
def rewrite_preview(
    ws: Workspace,
    store: Store,
    pattern: str,
    rewrite: str,
    *,
    language: str | None = None,
    glob: str | None = None,
    cap: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compute the full mechanical rewrite as a unified diff without touching
    the worktree. The patch is minted as an addressable ``blob:``; meta
    records the generation it was computed against (the apply guard)."""
    from ctx.execution import generation_hash

    b = binary()
    if b is None:
        raise EngineMissing("ast.rewrite requires ast-grep (no lossy fallback by design)")

    tail = ["run", "--pattern", pattern, "--rewrite", rewrite, "--json=stream"]
    if language:
        tail += ["--lang", language]
    if glob:
        tail += ["--globs", glob]
    tail.append(".")
    raw = _run_astgrep(ws, tail)

    # Group replacements per file; apply to in-memory copies; diff.
    per_file: dict[str, list[dict[str, Any]]] = {}
    for doc in _parse_stream(raw):
        rel = str(doc.get("file") or "").replace("\\", "/")
        if not rel or _ledger_path(rel) or ws.is_ignored(rel):
            continue
        repl = doc.get("replacement")
        rng = doc.get("range") or {}
        offs = (rng.get("byteOffset") or {})
        start, end = offs.get("start"), offs.get("end")
        if repl is None or start is None or end is None:
            continue
        per_file.setdefault(rel, []).append(
            {"start": int(start), "end": int(end), "replacement": str(repl)}
        )

    diffs: list[str] = []
    rows: list[dict[str, Any]] = []
    for rel in sorted(per_file):
        full = ws.confine(rel, must_exist=True)
        original = full.read_bytes()
        edits = sorted(per_file[rel], key=lambda e: (e["start"], e["end"]))
        # Reject overlaps outright: an overlapping mechanical rewrite is
        # ambiguous, and ambiguity is an error here, never a guess.
        for a, b2 in zip(edits, edits[1:]):
            if a["end"] > b2["start"]:
                raise RewriteError(f"overlapping rewrites in {rel} — refusing to guess")
        new = bytearray()
        pos = 0
        for e in edits:
            new += original[pos : e["start"]]
            new += e["replacement"].encode("utf-8")
            pos = e["end"]
        new += original[pos:]
        if bytes(new) == original:
            continue
        diff = difflib.unified_diff(
            original.decode("utf-8", "replace").splitlines(keepends=True),
            bytes(new).decode("utf-8", "replace").splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        diffs.append("".join(diff))
        rows.append({"file": rel, "edits": len(edits)})

    patch_text = "".join(diffs)
    patch_blob = store.put_blob(patch_text.encode("utf-8")) if patch_text else None
    gen = generation_hash(ws.root)
    meta = {
        "engine": engine_id(),
        "precision": "structural",
        "patch_blob": patch_blob,
        "generation": gen,
        "files": len(rows),
    }
    return rows[: max(1, cap)], meta


def rewrite_apply(
    ws: Workspace,
    store: Store,
    patch_blob: str,
    expect_generation: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Transactional apply of a previewed patch. Generation-guarded: refuses
    when the worktree changed since preview (``git apply`` then guarantees
    all-or-nothing on top)."""
    from ctx.execution import generation_hash

    if not patch_blob:
        raise RewriteError("apply requires the preview's patch_blob")
    gen_now = generation_hash(ws.root)
    if expect_generation and gen_now != expect_generation:
        raise RewriteError(
            "worktree generation changed since preview — re-run ast.rewrite.preview "
            "(refusing to apply a stale patch)"
        )
    patch = store.get_blob(str(patch_blob).removeprefix("blob:").removeprefix("sha256:"))
    if not patch.strip():
        return [], {"engine": engine_id(), "applied_files": 0, "note": "empty patch"}
    try:
        out = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=str(ws.root),
            input=patch,
            capture_output=True,
            timeout=_RUN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RewriteError(f"git apply failed to run: {e}") from e
    if out.returncode != 0:
        tail = out.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RewriteError("git apply rejected the patch: " + (tail[-1] if tail else "?"))
    files = sorted(
        {
            line[6:].strip()
            for line in patch.decode("utf-8", "replace").splitlines()
            if line.startswith("+++ b/")
        }
    )
    return (
        [{"file": f, "applied": True} for f in files],
        {"engine": engine_id(), "applied_files": len(files)},
    )


__all__ = [
    "EngineMissing",
    "RewriteError",
    "available",
    "binary",
    "lib_available",
    "engine_id",
    "ast_search",
    "rewrite_preview",
    "rewrite_apply",
]
