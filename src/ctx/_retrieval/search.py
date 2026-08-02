"""``ctx search``: multi-pattern bounded search with deterministic ordering
and explicit coverage reporting (SPEC §6.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ctx.execution import snapshot_file
from ctx.refs import Ref
from ctx import bounds
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


def _line_starts(text: str, line_nos: list[int]) -> dict[int, int]:
    """Offsets of the given ASCENDING 1-based line numbers, one forward pass.

    The inverse of :func:`_line_numbers`, and the ripgrep engine's bridge into
    the same line geometry: rg reports line NUMBERS, the renderer addresses
    lines by offset. One forward ``find`` per line actually crossed, so a file
    is walked once no matter how many hits it carries — and, unlike the
    ``splitlines()`` this replaced, nothing is materialized: a 3.2 MB file
    with one line costs a failed ``find``, not a 3.2 MB copy per rendered hit.

    ``\\n`` only, deliberately. That is the house line geometry everywhere
    else in this module (``tests/test_search_perf.py`` pins "a lone ``\\r`` is
    not a line break here") AND it is how ripgrep itself numbers lines, so the
    coordinate printed beside a context line now addresses the line rg meant.
    ``str.splitlines()`` also breaks on ``\\r``, ``\\v``, ``\\f``, ``\\x1c``,
    ``\\x85``, ``\\u2028`` and ``\\u2029``, which silently shifted the rg
    engine's context window against its own line numbers on any file
    containing one of them.
    """
    out: dict[int, int] = {}
    pos = 0
    cur = 1
    for n in line_nos:
        if n in out:
            continue
        while cur < n:
            nl = text.find("\n", pos)
            if nl == -1:
                pos = len(text)
                cur = n
                break
            pos = nl + 1
            cur += 1
        out[n] = pos
    return out


@dataclass(frozen=True, slots=True)
class RenderRow:
    """One hit, in the form the renderer needs and neither engine's own.

    Both engines produce these; :func:`_render_search` consumes them. That is
    what makes rg/Python output parity structural instead of two format
    strings maintained side by side (the parity assertions in
    ``tests/test_v03_libraries.py`` used to be the only thing holding them
    together).

    ``before``/``after`` are the context lines already extracted and bounded;
    ``text`` is the matched line. All three are produced by
    :func:`_hit_window`, so both engines cut them the same way.

    ``contextual`` is what earns the ``>`` marker, and it is NOT the same as
    "``before`` or ``after`` is non-empty": a ``--context 2`` hit on the only
    line of a file has no neighbours and still marks. It is also how the rg
    engine keeps its documented degradation — a context render whose file
    cannot be re-read falls back to rg's own copy of the matched line, with
    no marker.
    """

    target: str
    line_no: int
    col_a: int
    col_b: int
    text: str
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    contextual: bool = False


def _hit_window(
    target: SearchTarget, line_start: int, context: int
) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    """(before, matched, after) for one hit, every line bounded on extraction.

    One definition, both engines. ``context`` lines are walked outward with
    ``SearchTarget``'s offset primitives and read with :func:`_line_snippet`,
    so no line is ever materialized in full just to be truncated.
    """
    hit = _line_snippet(target.text, line_start)
    if not context:
        return (), hit, ()
    back: list[str] = []
    ls: int | None = line_start
    for _ in range(context):
        ls = target.prev_line_start(ls)  # type: ignore[arg-type]
        if ls is None:
            break
        back.append(_line_snippet(target.text, ls))
    back.reverse()
    fwd: list[str] = []
    ls = line_start
    for _ in range(context):
        ls = target.next_line_start(ls)  # type: ignore[arg-type]
        if ls is None:
            break
        fwd.append(_line_snippet(target.text, ls))
    return tuple(back), hit, tuple(fwd)


def _render_search(
    store: Store,
    ws: Workspace,
    ref: Ref,
    ref_text: str,
    patterns: list[str],
    rows: list[RenderRow],
    *,
    mode_all: bool,
    total: int,
    scanned: str,
    cap: int,
    snapshots: bool,
    telemetry_bytes: int,
) -> str:
    """The one ``ctx search`` rendering: header, hit body, coverage, result
    provenance, snapshot notes, continuation, emission and telemetry.

    ``rows`` are the hits to show (already capped and ordered), ``total`` the
    number of matches found, ``scanned`` the engine's own coverage line — the
    only part of the output the two engines legitimately disagree about,
    because "complete over corpus, N deep-searched" and "N targets, M lines"
    are different true statements about different work.
    """
    out: list[str] = [f"[ctx search {ref.display()}]"]
    out.append(
        "patterns: " + " ".join(repr(p) for p in patterns) + (" (all)" if mode_all else " (any)")
    )

    last_target = None
    for row in rows:
        if row.target != last_target:
            out.append(f"{row.target}:")
            last_target = row.target
        first = row.line_no - len(row.before)
        for i, line in enumerate(row.before):
            out.append(f"  L{first + i}: {line}")
        marker = ">" if row.contextual else " "
        out.append(f" {marker}L{row.line_no}: {row.text}")
        for i, line in enumerate(row.after, start=1):
            out.append(f"  L{row.line_no + i}: {line}")

    out.append("coverage:")
    out.append(scanned)
    out.append(
        f"  matches: {fmt_int(total)} · shown: {fmt_int(len(rows))}"
        + (" · truncated" if total > len(rows) else "")
    )
    sites_rows = [
        {"target": r.target, "line": r.line_no, "col_a": r.col_a, "col_b": r.col_b}
        for r in rows
    ]
    out.append(
        "result: blob:" + _mint_search_blob(store, ref_text, patterns, sites_rows, total)
    )

    if snapshots:
        snapshot_note: list[str] = []
        for label in sorted({r.target for r in rows}):
            try:
                snap = snapshot_file(store, ws, label)
                snapshot_note.append(f"  {label} → snapshot:{short_id(snap['id'])}")
            except Exception:
                pass
        if snapshot_note:
            out.append("snapshots:")
            out.extend(snapshot_note)

    continuation = None
    if total > len(rows):
        continuation = f"ctx search {ref_text} … --max-matches {min(total, cap * 2)}"
    result = _emit(
        ws, "\n".join(out), ws.config.budgets.result_tokens, continuation, handle=ref_text
    )
    record_telemetry(store, "search", telemetry_bytes, len(result.encode("utf-8")))
    return result


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
    # `or` reads an explicit 0 as unset, and a negative cap reached
    # `matches[:cap]` as a SUFFIX slice -- widening the output from the
    # argument whose job is to narrow it. Both spellings of the bounds defect
    # in one line: bounds.explicit answers "was it given", bounds.count
    # answers "is it a sane count".
    cap = bounds.count(bounds.explicit(max_matches, ws.config.budgets.max_matches))

    # MULTILINE keeps ^/$ line-anchored now that matching runs whole-text.
    flags = re.MULTILINE
    try:
        rxs = [
            re.compile(re.escape(p) if fixed else p, flags)
            for p in patterns
        ]
    except re.error as e:
        raise RetrievalError(f"invalid pattern: {e}") from e

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

    by_label = {t.label: t for t in targets}
    line_nos = _line_numbers(shown, by_label)
    rows: list[RenderRow] = []
    for hit in shown:
        t = by_label[hit.target]
        before, text_line, after = _hit_window(t, hit.line_start, context)
        rows.append(
            RenderRow(
                target=hit.target,
                line_no=line_nos[(hit.target, hit.line_start)],
                col_a=hit.col_a,
                col_b=hit.col_b,
                text=text_line,
                before=before,
                after=after,
                contextual=bool(context),
            )
        )

    return _render_search(
        store, ws, ref, ref_text, patterns, rows,
        mode_all=mode_all,
        total=len(matches),
        scanned=(
            f"  scanned: {fmt_int(considered)} targets · {fmt_int(scanned_lines)} lines"
            + (f" · {skipped_binary} binary skipped" if skipped_binary else "")
        ),
        cap=cap,
        # Snapshot-on-read for repo evidence (SPEC §6.3) — only repo targets
        # have a workspace file to pin.
        snapshots=ref.kind == "repo",
        telemetry_bytes=sum(len(t.text) for t in targets),
    )


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
    """The ripgrep engine's half: filter, then hand :func:`_render_search` the
    same ``RenderRow``s the Python engine hands it.

    Everything below the rows — header, coordinates, coverage frame, result
    blob, snapshot notes, continuation, emission, telemetry — is the shared
    renderer, so rg/Python parity is structural rather than two format
    strings kept equal by hand.
    """
    matches, scanned_line, bytes_searched = rg_result

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

    # Context rendering reads only the files that actually appear in output,
    # and reads each as a SearchTarget — the same line geometry the Python
    # engine uses. This replaced a `read_bytes().decode().splitlines()`, which
    # cost a full materialization of every line (a 3.2 MB copy per rendered
    # hit on a one-line file, the same giant-line cost fixed on the Python
    # path) and split on separators ripgrep does not count lines by.
    by_label: dict[str, SearchTarget] = {}
    starts: dict[str, dict[int, int]] = {}
    if context:
        for label in dict.fromkeys(m.target for m in shown):
            try:
                text = (ws.root / label).read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            by_label[label] = SearchTarget(label=label, text=text)
            starts[label] = _line_starts(
                text, sorted({m.line_no for m in shown if m.target == label})
            )

    rows: list[RenderRow] = []
    for m in shown:
        t = by_label.get(m.target)
        if context and t is not None and t.text:
            before, text_line, after = _hit_window(t, starts[m.target][m.line_no], context)
            contextual = True
        else:
            # No context asked for, or the file could not be re-read (or is
            # empty): fall back to rg's own copy of the matched line, unmarked.
            before, text_line, after = (), m.line[:_LINE_CHARS], ()
            contextual = False
        rows.append(
            RenderRow(
                target=m.target,
                line_no=m.line_no,
                col_a=m.col_a,
                col_b=m.col_b,
                text=text_line,
                before=before,
                after=after,
                contextual=contextual,
            )
        )

    return _render_search(
        store, ws, ref, ref_text, patterns, rows,
        mode_all=mode_all,
        total=len(matches),
        scanned=scanned_line,
        cap=cap,
        snapshots=True,  # the rg engine only ever runs on repo references
        telemetry_bytes=bytes_searched,
    )
