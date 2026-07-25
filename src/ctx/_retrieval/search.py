"""``ctx search``: multi-pattern bounded search with deterministic ordering
and explicit coverage reporting (SPEC §6.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ctx.execution import snapshot_file
from ctx.refs import Ref
from ctx.store import Store, canonical_json
from ctx.textutil import fmt_int, short_id
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
    # Span-precise columns (M-K1): 1-based, half-open [col_a, col_b)
    # character columns of the leftmost match on the line. 0 = unknown.
    col_a: int = 0
    col_b: int = 0


_LINE_CHARS = 200  # rendered width cap for one matched/context line


def _line_snippet(text: str, line_start: int, limit: int = _LINE_CHARS) -> str:
    """The line at ``line_start``, truncated to ``limit`` characters.

    Equivalent to ``target.line_text_at(line_start)[:limit]`` but it never
    materializes the full line first: a minified bundle is one enormous
    "line", and slicing 3.2 MB out just to keep 200 characters costs a
    megabyte-scale copy per rendered hit. Bounded by construction instead.
    """
    end = text.find("\n", line_start, line_start + limit)
    return text[line_start : end if end != -1 else min(len(text), line_start + limit)]


def _line_numbers(shown: list[SearchHit], by_label: dict[str, SearchTarget]) -> dict[
    tuple[str, int], int
]:
    """1-based line numbers for the shown hits, one forward pass per target.

    ``shown`` is sorted by (target, line_start), so newlines are counted once
    between consecutive line starts. Resolving each hit independently with
    ``target.line_no_of`` re-counted from offset 0 every time — and did it
    twice per hit (rendered line + sites row): measured 94 ms for 79 hits
    spread over a 3.2 MB file, 1.2 ms with the forward pass.
    """
    out: dict[tuple[str, int], int] = {}
    label: str | None = None
    text = ""
    prev = 0
    n = 1
    for hit in shown:
        if hit.target != label:
            label, text, prev, n = hit.target, by_label[hit.target].text, 0, 1
        n += text.count("\n", prev, hit.line_start)
        prev = hit.line_start
        out[(hit.target, hit.line_start)] = n
    return out


def _mint_search_blob(
    store: Store,
    ref_text: str,
    patterns: list[str],
    sites: list[dict],
    total: int,
) -> str:
    """Per-result provenance (M-K1): the shown coordinates as one derived
    canonical-JSON blob, so a search is citable as a single handle (parity
    with ``ctx q``'s final-stream minting). Returns the short blob id."""
    payload = {
        "format": "ctx.search/v1",
        "ref": ref_text,
        "patterns": list(patterns),
        "total": int(total),
        "sites": sites,
    }
    return short_id(store.put_blob(canonical_json(payload)))


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
        targets = [
            SearchTarget(
                label=f"blob:{short_id(blob_id)}", text=_stream_text(store, blob_id)
            )
        ]
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
        # record per line via the line-start offset. The LEFTMOST match on
        # the line wins (rg submatch parity; ties break on pattern index),
        # and its span rides the hit as 1-based [col_a, col_b) columns.
        # Line numbers are resolved later, only for shown matches.
        per_line: dict[int, tuple[int, int, int]] = {}  # start → (pi, a, b)
        text = target.text
        for pi, rx in enumerate(rxs):
            # ``finditer`` yields matches in ascending start order, so the
            # enclosing line start only ever moves forward: advance a cursor
            # by one forward memchr per line actually crossed. The previous
            # form asked ``text.rfind("\n", 0, m.start())`` per match, an
            # O(offset) backward rescan each time — quadratic exactly when a
            # file has few newlines to stop the scan. Measured on a 3.2 MB
            # newline-free minified bundle (67,369 matches): 2811 ms in this
            # loop before, 8 ms after. See tests/test_search_perf.py.
            line_start = 0
            next_nl = text.find("\n")
            for m in rx.finditer(text):
                start = m.start()
                while next_nl != -1 and next_nl < start:
                    line_start = next_nl + 1
                    next_nl = text.find("\n", line_start)
                prev = per_line.get(line_start)
                if prev is None or (start, pi) < (prev[1], prev[0]):
                    per_line[line_start] = (pi, start, m.end())
        for line_start, (pi, a, b) in per_line.items():
            matches.append(
                SearchHit(
                    target.label,
                    line_start,
                    pi,
                    col_a=a - line_start + 1,
                    col_b=b - line_start + 1,
                )
            )

    matches.sort(key=lambda m: (m.target, m.line_start, m.pattern_index))
    shown = matches[:cap]

    # Snapshot-on-read for repo evidence (SPEC §6.3).
    if ref.kind == "repo" and shown:
        for label in sorted({m.target for m in shown}):
            try:
                snap = snapshot_file(store, ws, label)
                snapshot_note.append(
                    f"  {label} → snapshot:{short_id(snap['id'])}"
                )
            except Exception:
                pass

    out: list[str] = [f"[ctx search {ref.display()}]"]
    out.append("patterns: " + " ".join(repr(p) for p in patterns) + (" (all)" if mode_all else " (any)"))
    last_target = None
    by_label = {t.label: t for t in targets}
    line_nos = _line_numbers(shown, by_label)
    for hit in shown:
        if hit.target != last_target:
            out.append(f"{hit.target}:")
            last_target = hit.target
        t = by_label[hit.target]
        line_no = line_nos[(hit.target, hit.line_start)]
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
                out.append(f"  L{line_no - len(back) + i}: {_line_snippet(t.text, ls_k)}")
            out.append(f" >L{line_no}: {_line_snippet(t.text, hit.line_start)}")
            for i, ls_k in enumerate(fwd, start=1):
                out.append(f"  L{line_no + i}: {_line_snippet(t.text, ls_k)}")
        else:
            out.append(f"  L{line_no}: {_line_snippet(t.text, hit.line_start)}")

    out.append("coverage:")
    out.append(
        f"  scanned: {fmt_int(considered)} targets · {fmt_int(scanned_lines)} lines"
        + (f" · {skipped_binary} binary skipped" if skipped_binary else "")
    )
    out.append(
        f"  matches: {fmt_int(len(matches))} · shown: {fmt_int(len(shown))}"
        + (" · truncated" if len(matches) > len(shown) else "")
    )
    sites_rows = [
        {
            "target": h.target,
            "line": line_nos[(h.target, h.line_start)],
            "col_a": h.col_a,
            "col_b": h.col_b,
        }
        for h in shown
    ]
    out.append(
        "result: blob:"
        + _mint_search_blob(store, ref_text, patterns, sites_rows, len(matches))
    )
    if snapshot_note:
        out.append("snapshots:")
        out.extend(snapshot_note)

    continuation = None
    if len(matches) > len(shown):
        continuation = f"ctx search {ref_text} … --max-matches {min(len(matches), cap * 2)}"
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation, handle=ref_text)
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
    sites_rows = [
        {"target": m.target, "line": m.line_no, "col_a": m.col_a, "col_b": m.col_b}
        for m in shown
    ]
    out.append(
        "result: blob:"
        + _mint_search_blob(store, ref_text, patterns, sites_rows, len(matches))
    )

    snapshot_note: list[str] = []
    for label in sorted({m.target for m in shown}):
        try:
            snap = snapshot_file(store, ws, label)
            snapshot_note.append(
                f"  {label} → snapshot:{short_id(snap['id'])}"
            )
        except Exception:
            pass
    if snapshot_note:
        out.append("snapshots:")
        out.extend(snapshot_note)

    continuation = None
    if len(matches) > len(shown):
        continuation = f"ctx search {ref_text} … --max-matches {min(len(matches), cap * 2)}"
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation, handle=ref_text)
    record_telemetry(store, "search", bytes_searched, len(result.encode("utf-8")))
    return result
