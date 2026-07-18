"""Symbol-addressed code verbs (ROADMAP M-B): ``ctx def / refs / diag``.

Meaning-indexed access replaces byte dumps: resolve one definition, cite one
span. Backend policy: **jedi** (pure-Python, library-mode) when importable
and ``CTX_CODE_ENGINE`` is not ``ast``; otherwise the existing stdlib-ast
machinery. The engine in use is disclosed in every output header. Provenance
contract: every emitted site is snapshot-backed and (for definitions)
span-tagged, so evidence stays resolvable after the worktree changes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ctx.execution import snapshot_file
from ctx.retrieval import (
    RetrievalError,
    _emit,
    _python_symbol_span,
    _resolve_repo_targets,
    record_telemetry,
)
from ctx.store import Store
from ctx.textutil import fmt_bytes, fmt_int
from ctx.workspace import Workspace

_ENGINE_JEDI = "jedi"
_ENGINE_AST = "ast"
_DIAG_MAX_FILES = 200
_LINE_CAP = 160


# ----------------------------------------------------------------- engine
def _select_engine() -> str:
    if os.environ.get("CTX_CODE_ENGINE") == _ENGINE_AST:
        return _ENGINE_AST
    try:
        import jedi  # noqa: F401
    except Exception:
        return _ENGINE_AST
    return _ENGINE_JEDI


def _within_root(ws: Workspace, module_path: object) -> str | None:
    """Repo-relative POSIX path when inside the workspace, else None."""
    if module_path is None:
        return None
    try:
        return Path(str(module_path)).resolve().relative_to(ws.root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


# ----------------------------------------------------------------- target
def _parse_target(target: str) -> tuple[str, str]:
    """``repo:path/file.py:Symbol.dotted`` or ``repo:path/file.py --symbol
    Symbol.dotted`` — one string, two coordinates."""
    grammar = (
        "target grammar: repo:<path>:<Symbol.dotted> "
        "(or 'repo:<path> --symbol <Symbol.dotted>')"
    )
    t = target.strip()
    sym: str | None = None
    if "--symbol" in t:
        left, _, right = t.partition("--symbol")
        t, sym = left.strip(), right.strip()
    if t.startswith("repo:"):
        t = t[len("repo:") :]
    if sym is None:
        if ":" not in t:
            raise RetrievalError(f"missing symbol; {grammar}")
        t, sym = t.rsplit(":", 1)
    rel = t.strip().strip("/")
    sym = sym.strip()
    if not rel or not sym or not all(p.isidentifier() for p in sym.split(".")):
        raise RetrievalError(f"unparseable target {target!r}; {grammar}")
    if ".." in rel.split("/"):
        raise RetrievalError(f"target path must not contain '..': {rel!r}")
    return rel, sym


# -------------------------------------------------------------------- def
def _jedi_def(ws: Workspace, rel: str, symbol: str) -> tuple[str, int, int, str]:
    """(def_rel, start_line, end_line, kind) via jedi; raises on no match."""
    import jedi

    project = jedi.Project(str(ws.root))
    script = jedi.Script(path=str(ws.root / rel), project=project)
    want = symbol.split(".")

    cands = []
    for n in script.get_names(all_scopes=True, definitions=True):
        if n.type not in ("function", "class"):
            continue
        dotted = (n.full_name or n.name or "").split(".")
        if dotted[-len(want) :] == want:
            cands.append(n)
    if not cands:
        # Follow imports: the named file may only re-export the symbol.
        for hit in script.search(symbol):
            for n in hit.goto(follow_imports=True):
                dotted = (n.full_name or n.name or "").split(".")
                if n.type in ("function", "class") and dotted[-len(want) :] == want:
                    cands.append(n)
    if not cands:
        raise RetrievalError(
            f"definition {symbol!r} not found from {rel} (engine jedi)"
        )
    cands.sort(key=lambda n: (str(n.module_path or ""), n.line or 0, n.column or 0))
    pick = cands[0]
    def_rel = _within_root(ws, pick.module_path)
    if def_rel is None:
        raise RetrievalError(
            f"definition of {symbol!r} resolves outside the workspace"
        )
    a = pick.get_definition_start_position()[0]
    b = pick.get_definition_end_position()[0]
    return def_rel, a, b, pick.type


def _ast_def(ws: Workspace, rel: str, symbol: str) -> tuple[str, int, int, str]:
    source = ws.confine(rel, must_exist=True).read_bytes().decode("utf-8", "replace")
    span = _python_symbol_span(source, symbol)
    if span is None:
        raise RetrievalError(
            f"symbol {symbol!r} not found in {rel} "
            "(engine ast resolves definitions within the named file only)"
        )
    lines = source.splitlines()
    first = lines[span[0] - 1].strip() if span[0] <= len(lines) else ""
    kind = "class" if first.startswith("class ") else "function"
    return rel, span[0], span[1], kind


def cmd_def(store: Store, ws: Workspace, target: str) -> str:
    """Definition site for one symbol: file+line span, snapshot, minted
    region span, and the body when it fits ``max_inline_lines``."""
    budget = ws.config.budgets
    rel, symbol = _parse_target(target)
    ws.confine(rel, must_exist=True)

    engine = _select_engine()
    if engine == _ENGINE_JEDI:
        try:
            def_rel, a, b, kind = _jedi_def(ws, rel, symbol)
        except RetrievalError:
            raise
        except Exception:
            engine = _ENGINE_AST
    if engine == _ENGINE_AST:
        def_rel, a, b, kind = _ast_def(ws, rel, symbol)

    snap = snapshot_file(store, ws, def_rel)
    snap_short = str(snap["id"]).removeprefix("sha256:")[:12]
    blob = str(snap["blob"]).removeprefix("sha256:")
    sid = store.register_span(blob, "region", a=a, b=b, note=f"def {symbol}")

    data = store.get_blob(blob)
    lines = data.decode("utf-8", "replace").splitlines()
    body = lines[a - 1 : b]
    n_lines = b - a + 1

    out = [f"[ctx def repo:{rel}:{symbol} · engine {engine}]"]
    out.append(f"definition: repo:{def_rel} L{a}:{b} ({kind})")
    out.append(f"snapshot: snapshot:{snap_short}")
    out.append(
        f"span: {sid} (region L{a}:{b}) · resolve: ctx get repo:{def_rel} --span {sid}"
    )
    if n_lines <= budget.max_inline_lines:
        out.append("body (complete):")
        shown = body
    else:
        out.append(
            f"body: {fmt_int(n_lines)} lines — showing first 10 · "
            f"full body: ctx get repo:{def_rel} --span {sid}"
        )
        shown = body[:10]
    out.extend(f"L{a + i}: {ln}" for i, ln in enumerate(shown))

    result = _emit(ws, "\n".join(out), budget.result_tokens)
    record_telemetry(store, "code", len(data), len(result.encode("utf-8")))
    return result


# ------------------------------------------------------------------- refs
def _jedi_refs(ws: Workspace, symbol: str) -> tuple[list[tuple[str, int, str]], int]:
    """Reference sites across the workspace via jedi; raises to trigger the
    textual fallback (no definition found, engine trouble)."""
    import jedi

    project = jedi.Project(str(ws.root))
    defs = [
        n
        for n in project.search(symbol)
        if n.line is not None and _within_root(ws, n.module_path) is not None
    ]
    if not defs:
        raise RetrievalError(f"no definition found for {symbol!r}")
    defs.sort(key=lambda n: (str(n.module_path), n.line or 0, n.column or 0))
    d = defs[0]

    script = jedi.Script(path=str(d.module_path), project=project)
    refs = script.get_references(d.line, d.column, include_builtins=False)

    sites: list[tuple[str, int, str]] = []
    texts: dict[str, list[str]] = {}
    scanned = 0
    for r in refs:
        rel = _within_root(ws, r.module_path)
        if rel is None or r.line is None:
            continue
        if rel not in texts:
            try:
                raw = (ws.root / rel).read_bytes()
            except OSError:
                continue
            scanned += len(raw)
            texts[rel] = raw.decode("utf-8", "replace").splitlines()
        lines = texts[rel]
        text = lines[r.line - 1] if 0 < r.line <= len(lines) else ""
        sites.append((rel, r.line, text.rstrip()))
    return sites, scanned


def _ast_refs(
    store: Store, ws: Workspace, symbol: str, scope_path: str | None
) -> tuple[list[tuple[str, int, str]], int]:
    """Word-boundary textual references over ``*.py`` (labeled, not semantic)."""
    from ctx.refs import parse_ref

    ref = parse_ref("repo:" + (scope_path or ""))
    targets, _, _ = _resolve_repo_targets(store, ws, ref, glob="**/*.py", scope=None)
    rx = re.compile(rf"\b{re.escape(symbol.split('.')[-1])}\b")
    sites: list[tuple[str, int, str]] = []
    scanned = 0
    for t in targets:
        scanned += len(t.text)
        for i, ln in enumerate(t.text.splitlines(), start=1):
            if rx.search(ln):
                sites.append((t.label, i, ln.rstrip()))
    return sites, scanned


def cmd_refs(
    store: Store, ws: Workspace, symbol: str, scope_path: str | None = None
) -> str:
    """Reference sites as coordinates: deterministic (path, line) order,
    budget-capped with continuation, snapshot-on-first-cite per file."""
    budget = ws.config.budgets
    cap = budget.max_matches

    engine = _select_engine()
    label = engine
    if engine == _ENGINE_JEDI:
        try:
            sites, scanned = _jedi_refs(ws, symbol)
        except Exception:
            engine = _ENGINE_AST
    if engine == _ENGINE_AST:
        label = "ast (textual)"
        sites, scanned = _ast_refs(store, ws, symbol, scope_path)

    if scope_path:
        pfx = scope_path.strip("/")
        sites = [s for s in sites if s[0] == pfx or s[0].startswith(pfx + "/")]

    uniq: dict[tuple[str, int], str] = {}
    for rel, line, text in sites:
        uniq.setdefault((rel, line), text)
    ordered = sorted(uniq.items())
    shown = ordered[:cap]
    truncated = len(ordered) > len(shown)

    scope_note = f" · path {scope_path}" if scope_path else ""
    out = [f"[ctx refs {symbol}{scope_note} · engine {label}]"]
    for (rel, line), text in shown:
        out.append(f"repo:{rel}:L{line}: {text[:_LINE_CAP]}")
    out.append("coverage:")
    out.append(
        f"  sites: {fmt_int(len(ordered))} · shown: {fmt_int(len(shown))}"
        + (" · truncated" if truncated else "")
    )

    snapshot_note: list[str] = []
    for rel in sorted({rel for (rel, _), _ in shown}):
        try:
            snap = snapshot_file(store, ws, rel)
            snapshot_note.append(
                f"  {rel} → snapshot:{str(snap['id']).removeprefix('sha256:')[:12]}"
            )
        except Exception:
            pass
    if snapshot_note:
        out.append("snapshots:")
        out.extend(snapshot_note)

    continuation = None
    if truncated:
        continuation = (
            f"ctx refs {symbol} --path <subtree> "
            f"(narrow scope; {fmt_int(len(ordered) - len(shown))} sites omitted)"
        )
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation)
    record_telemetry(store, "code", scanned, len(result.encode("utf-8")))
    return result


# ------------------------------------------------------------------- diag
def _pyflakes_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("pyflakes") is not None


_DIAG_LINE_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?:\d+:?)?\s*(?P<msg>.+)$")


def _pyflakes_diags(
    ws: Workspace, rels: list[str]
) -> list[tuple[str, int, str, str]] | None:
    """(rel, line, severity, message) via ``python -m pyflakes``; stdout is
    lint (warning), stderr is syntax trouble (error). None → fall back."""
    import subprocess
    import sys as _sys

    try:
        proc = subprocess.run(
            [_sys.executable, "-m", "pyflakes", *rels],
            cwd=ws.root,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    diags: list[tuple[str, int, str, str]] = []
    for severity, stream in (("warning", proc.stdout), ("error", proc.stderr)):
        for raw in stream.decode("utf-8", "replace").splitlines():
            m = _DIAG_LINE_RE.match(raw)
            if not m:
                continue  # source-echo/caret lines under a syntax error
            rel = m.group("path").replace("\\", "/").removeprefix("./")
            if rel not in rels:
                continue
            diags.append((rel, int(m.group("line")), severity, m.group("msg").strip()))
    return diags


def _py_compile_diags(ws: Workspace, rels: list[str]) -> list[tuple[str, int, str, str]]:
    """Syntax-only fallback: the same check ``py_compile`` performs, run
    in-process so no ``.pyc`` files land in the worktree."""
    diags: list[tuple[str, int, str, str]] = []
    for rel in rels:
        try:
            source = (ws.root / rel).read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        try:
            compile(source, rel, "exec")
        except SyntaxError as e:
            diags.append((rel, int(e.lineno or 0), "error", e.msg or "syntax error"))
        except ValueError as e:  # null bytes
            diags.append((rel, 0, "error", str(e)))
    return diags


def cmd_diag(store: Store, ws: Workspace, path: str | None = None) -> str:
    """Deterministic diagnostics digest over the workspace's Python files:
    count by severity, first 10 sites, explicit coverage and checker."""
    budget = ws.config.budgets
    engine = _select_engine()

    rels = [r for r in ws.list_files(path) if r.endswith(".py")]
    total = len(rels)
    checked = rels[:_DIAG_MAX_FILES]
    scanned = 0
    for rel in checked:
        try:
            scanned += (ws.root / rel).stat().st_size
        except OSError:
            pass

    checker = "pyflakes" if _pyflakes_available() else "py_compile"
    diags = _pyflakes_diags(ws, checked) if checker == "pyflakes" else None
    if diags is None:
        checker = "py_compile"
        diags = _py_compile_diags(ws, checked)
    diags.sort(key=lambda d: (d[0], d[1], d[2], d[3]))

    by_sev: dict[str, int] = {}
    for _, _, sev, _ in diags:
        by_sev[sev] = by_sev.get(sev, 0) + 1

    scope_note = f"repo:{path}" if path else "repo:"
    out = [f"[ctx diag {scope_note} · engine {engine} · checker {checker}]"]
    if diags:
        counts = " · ".join(f"{sev} {fmt_int(by_sev[sev])}" for sev in sorted(by_sev))
        out.append(f"diagnostics (exact): {fmt_int(len(diags))} · {counts}")
        for rel, line, _, msg in diags[:10]:
            out.append(f"repo:{rel}:L{line}: {msg[:_LINE_CAP]}")
        if len(diags) > 10:
            out.append(f"… +{fmt_int(len(diags) - 10)} more diagnostics")
    else:
        out.append("diagnostics (exact): none")
    out.append("coverage:")
    out.append(
        f"  checked: {fmt_int(len(checked))} of {fmt_int(total)} python files"
        f" · {fmt_bytes(scanned)}"
        + (" · truncated at file cap" if total > len(checked) else "")
    )

    result = _emit(ws, "\n".join(out), budget.result_tokens)
    record_telemetry(store, "code", scanned, len(result.encode("utf-8")))
    return result
