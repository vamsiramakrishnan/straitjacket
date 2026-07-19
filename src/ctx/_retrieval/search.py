"""``ctx search``: multi-pattern bounded search with deterministic ordering
and explicit coverage reporting (SPEC §6.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ctx.execution import snapshot_file
from ctx.refs import Ref
from ctx.store import Store
from ctx.textutil import fmt_int
from ctx.workspace import Workspace

from .common import RetrievalError, _emit, _parse, _route_workspace
from .rg_engine import RgMatch, _rg_available, _rg_repo_search
from .targets import SearchTarget, _resolve_repo_targets, _resolve_run_targets, _stream_text
from .telemetry import record_telemetry


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One matched line within a search target.

    Named ``SearchHit`` rather than ``Match`` (its pre-split name) because a
    same-named local of the *stdlib*'s ``re.Match`` lives in the same
    function's scope (``for m in rx.finditer(text)``) — the identical name
    confused mypy's flow analysis into unifying the two unrelated types,
    the exact ``Match``/``Selector`` union-flow residual debt bf48ba3c4e
    named as blocked on "a refactor [that] splits retrieval.py". The facade
    still re-exports the old name (``ctx.retrieval.Match``) as an alias —
    nothing outside this module ever imported it, but the alias costs
    nothing and keeps the byte-compatible-facade guarantee absolute.
    """

    target: str
    line_start: int  # char offset; line number/text computed only if shown
    pattern_index: int


def search(
    store: Store,
    ws: Workspace,
    ref_text: str,
    patterns: list[str],
    *,
    fixed: bool = False,
    mode_all: bool = False,
    context: int = 0,
    glob: str | None = None,
    scope: str | None = None,
    max_matches: int | None = None,
) -> str:
    """Multi-pattern bounded search with deterministic ordering by
    (target, coordinate, pattern index) and explicit coverage reporting."""
    if not patterns:
        raise RetrievalError("at least one pattern is required")
    ref = _parse(ref_text)
    store, ws = _route_workspace(store, ws, ref)
    budget = ws.config.budgets
    cap = max_matches or budget.max_matches

    # MULTILINE keeps ^/$ line-anchored now that matching runs whole-text.
    flags = re.MULTILINE
    try:
        rxs = [
            re.compile(re.escape(p) if fixed else p, flags)
            for p in patterns
        ]
    except re.error as e:
        raise RetrievalError(f"invalid pattern: {e}") from e

    snapshot_note: list[str] = []

    # -------- repo searches prefer ripgrep when installed (auto-fallback)
    if ref.kind == "repo" and _rg_available():
        if scope:
            scoped = ws.config.scopes.get(scope)
            if not scoped:
                raise RetrievalError(
                    f"unknown scope {scope!r}; configured: {sorted(ws.config.scopes) or 'none'}"
                )
            roots = list(scoped)
        else:
            roots = [ref.path] if ref.path else []
        for r in roots:
            ws.confine(r, must_exist=True)  # workspace confinement still applies
        rg_result = _rg_repo_search(
            ws, roots, patterns, rxs, fixed=fixed, glob=glob
        )
        if rg_result is not None:
            return _render_rg_search(
                store, ws, ref, ref_text, patterns, rg_result,
                mode_all=mode_all, context=context, cap=cap, rxs=rxs,
            )
        # else: fall through to the Python engine

    skipped_binary = 0
    if ref.kind == "run":
        targets, skipped_binary = _resolve_run_targets(store, ref)
        considered = len(targets) + skipped_binary
    elif ref.kind == "blob":
        blob_id = store.resolve_id(ref.id or "", kinds=("blob",))
        targets = [SearchTarget(label=f"blob:{blob_id[:12]}", text=_stream_text(store, blob_id))]
        considered = 1
    elif ref.kind == "repo":
        targets, considered, skipped_binary = _resolve_repo_targets(
            store, ws, ref, glob=glob, scope=scope
        )
    else:
        raise RetrievalError(f"cannot search reference kind {ref.kind!r}")

    matches: list[SearchHit] = []
    scanned_lines = 0
    for target in targets:
        scanned_lines += target.n_lines
        if mode_all and not all(rx.search(target.text) for rx in rxs):
            continue
        # C-speed scan: finditer per pattern over the whole text; dedup to one
        # record per line via the line-start offset (lowest pattern index
        # wins). Line numbers are resolved later, only for shown matches.
        per_line: dict[int, int] = {}
        text = target.text
        for pi, rx in enumerate(rxs):
            for m in rx.finditer(text):
                line_start = text.rfind("\n", 0, m.start()) + 1
                prev = per_line.get(line_start)
                if prev is None or pi < prev:
                    per_line[line_start] = pi
        for line_start, pi in per_line.items():
            matches.append(SearchHit(target.label, line_start, pi))

    matches.sort(key=lambda m: (m.target, m.line_start, m.pattern_index))
    shown = matches[:cap]

    # Snapshot-on-read for repo evidence (SPEC §6.3).
    if ref.kind == "repo" and shown:
        for label in sorted({m.target for m in shown}):
            try:
                snap = snapshot_file(store, ws, label)
                snapshot_note.append(
                    f"  {label} → snapshot:{str(snap['id']).removeprefix('sha256:')[:12]}"
                )
            except Exception:
                pass

    out: list[str] = [f"[ctx search {ref.display()}]"]
    out.append("patterns: " + " ".join(repr(p) for p in patterns) + (" (all)" if mode_all else " (any)"))
    last_target = None
    by_label = {t.label: t for t in targets}
    for hit in shown:
        if hit.target != last_target:
            out.append(f"{hit.target}:")
            last_target = hit.target
        t = by_label[hit.target]
        line_no = t.line_no_of(hit.line_start)
        if context:
            back: list[int] = []
            ls: int | None = hit.line_start
            for _ in range(context):
                ls = t.prev_line_start(ls)  # type: ignore[arg-type]
                if ls is None:
                    break
                back.append(ls)
            back.reverse()
            fwd: list[int] = []
            ls = hit.line_start
            for _ in range(context):
                ls = t.next_line_start(ls)  # type: ignore[arg-type]
                if ls is None:
                    break
                fwd.append(ls)
            for i, ls_k in enumerate(back):
                out.append(f"  L{line_no - len(back) + i}: {t.line_text_at(ls_k)[:200]}")
            out.append(f" >L{line_no}: {t.line_text_at(hit.line_start)[:200]}")
            for i, ls_k in enumerate(fwd, start=1):
                out.append(f"  L{line_no + i}: {t.line_text_at(ls_k)[:200]}")
        else:
            out.append(f"  L{line_no}: {t.line_text_at(hit.line_start)[:200]}")

    out.append("coverage:")
    out.append(
        f"  scanned: {fmt_int(considered)} targets · {fmt_int(scanned_lines)} lines"
        + (f" · {skipped_binary} binary skipped" if skipped_binary else "")
    )
    out.append(
        f"  matches: {fmt_int(len(matches))} · shown: {fmt_int(len(shown))}"
        + (" · truncated" if len(matches) > len(shown) else "")
    )
    if snapshot_note:
        out.append("snapshots:")
        out.extend(snapshot_note)

    continuation = None
    if len(matches) > len(shown):
        continuation = f"ctx search {ref_text} … --max-matches {min(len(matches), cap * 2)}"
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation)
    record_telemetry(
        store, "search", sum(len(t.text) for t in targets), len(result.encode("utf-8"))
    )
    return result


def _render_rg_search(
    store: Store,
    ws: Workspace,
    ref: Ref,
    ref_text: str,
    patterns: list[str],
    rg_result: tuple[list[RgMatch], str, int],
    *,
    mode_all: bool,
    context: int,
    cap: int,
    rxs: list["re.Pattern[str]"],
) -> str:
    matches, scanned_line, bytes_searched = rg_result
    budget = ws.config.budgets

    if mode_all and matches:
        by_target: dict[str, list[RgMatch]] = {}
        for m in matches:
            by_target.setdefault(m.target, []).append(m)
        keep: set[str] = set()
        for target, ms in by_target.items():
            lines = [m.line for m in ms]
            if all(any(rx.search(ln) for ln in lines) for rx in rxs):
                keep.add(target)
        matches = [m for m in matches if m.target in keep]

    shown = matches[:cap]

    out: list[str] = [f"[ctx search {ref.display()}]"]
    out.append(
        "patterns: " + " ".join(repr(p) for p in patterns) + (" (all)" if mode_all else " (any)")
    )

    # Context rendering reads only the files that actually appear in output.
    file_lines: dict[str, list[str]] = {}
    if context:
        for label in dict.fromkeys(m.target for m in shown):
            try:
                file_lines[label] = (
                    (ws.root / label).read_bytes().decode("utf-8", "replace").splitlines()
                )
            except OSError:
                file_lines[label] = []

    last_target = None
    for m in shown:
        if m.target != last_target:
            out.append(f"{m.target}:")
            last_target = m.target
        if context and file_lines.get(m.target):
            lines = file_lines[m.target]
            a = max(1, m.line_no - context)
            b = min(len(lines), m.line_no + context)
            for n in range(a, b + 1):
                marker = ">" if n == m.line_no else " "
                out.append(f" {marker}L{n}: {lines[n - 1][:200]}")
        else:
            out.append(f"  L{m.line_no}: {m.line[:200]}")

    out.append("coverage:")
    out.append(scanned_line)
    out.append(
        f"  matches: {fmt_int(len(matches))} · shown: {fmt_int(len(shown))}"
        + (" · truncated" if len(matches) > len(shown) else "")
    )

    snapshot_note: list[str] = []
    for label in sorted({m.target for m in shown}):
        try:
            snap = snapshot_file(store, ws, label)
            snapshot_note.append(
                f"  {label} → snapshot:{str(snap['id']).removeprefix('sha256:')[:12]}"
            )
        except Exception:
            pass
    if snapshot_note:
        out.append("snapshots:")
        out.extend(snapshot_note)

    continuation = None
    if len(matches) > len(shown):
        continuation = f"ctx search {ref_text} … --max-matches {min(len(matches), cap * 2)}"
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation)
    record_telemetry(store, "search", bytes_searched, len(result.encode("utf-8")))
    return result
