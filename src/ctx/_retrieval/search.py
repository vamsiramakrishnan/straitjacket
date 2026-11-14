"""``ctx search``: multi-pattern bounded search with deterministic ordering
and explicit coverage reporting (SPEC §6.3)."""

from __future__ import annotations

from ctx.textutil import clip_redacting, REDACTION_CONTEXT_CHARS  # redaction-aware clips
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


def _line_snippet(text: str, line_start: int, limit: int = _LINE_CHARS, policy=None) -> str:
    """The line at ``line_start``, truncated to ``limit`` characters.

    Equivalent to ``target.line_text_at(line_start)[:limit]`` but it never
    materializes the full line first: a minified bundle is one enormous
    "line", and slicing 3.2 MB out just to keep 200 characters costs a
    megabyte-scale copy per rendered hit. Bounded by construction instead.
    """
    # Read a bounded window past the cut so a secret straddling it is judged
    # whole (clip_redacting blanks its visible part, same width).
    reach = limit + REDACTION_CONTEXT_CHARS
    end = text.find("\n", line_start, line_start + reach)
    line = text[line_start : end if end != -1 else min(len(text), line_start + reach)]
    return clip_redacting(line, limit, policy)


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
    target: SearchTarget, line_start: int, context: int, policy=None
) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    """(before, matched, after) for one hit, every line bounded on extraction.

    One definition, both engines. ``context`` lines are walked outward with
    ``SearchTarget``'s offset primitives and read with :func:`_line_snippet`,
    so no line is ever materialized in full just to be truncated. A line
    inside a private-key block (its BEGIN found by plain search up to 16 KiB
    back) is blanked at the same width: a key's body lines match no pattern
    on their own.
    """
    starts = [line_start]
    back: list[str] = []
    ls: int | None = line_start
    for _ in range(context):
        ls = target.prev_line_start(ls)  # type: ignore[arg-type]
        if ls is None:
            break
        back.append(_line_snippet(target.text, ls, policy=policy))
        starts.insert(0, ls)
    back.reverse()
    hit = _line_snippet(target.text, line_start, policy=policy)
    fwd: list[str] = []
    ls = line_start
    for _ in range(context):
        ls = target.next_line_start(ls)  # type: ignore[arg-type]
        if ls is None:
            break
        fwd.append(_line_snippet(target.text, ls, policy=policy))
        starts.append(ls)
    shown = _blank_private_key_lines(target.text, starts, back + [hit] + fwd, policy)
    nb = len(back)
    return tuple(shown[:nb]), shown[nb], tuple(shown[nb + 1:])


def _blank_private_key_lines(text: str, starts: list[int], shown: list[str], policy) -> list[str]:
    """``shown`` (lines starting at ``starts``) with any part inside a
    private-key block blanked at the same width. Identity without one."""
    from ctx.textutil import PEM_LOOKBEHIND_CHARS, active_patterns, pem_spans

    lo = max(0, starts[0] - PEM_LOOKBEHIND_CHARS)
    hi = min(len(text), starts[-1] + len(shown[-1]) + 64)
    if "PRIVATE KEY-----" not in text[lo:hi]:
        return shown
    if "private-key" not in {n for n, _ in active_patterns(policy)}:
        return shown
    spans = pem_spans(text, lo, hi)
    if not spans:
        return shown
    out = []
    for ls, line in zip(starts, shown):
        le = ls + len(line)
        chars = list(line)
        for a, b in spans:
            for i in range(max(a, ls), min(b, le)):
                chars[i - ls] = "*"
        out.append("".join(chars))
    return out


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
    filters: str = "",
) -> str:
    """The one ``ctx search`` rendering: header, hit body, coverage, result
    provenance, snapshot notes, continuation, emission and telemetry.

    ``rows`` are the hits to show (already capped and ordered), ``total`` the
    number of matches found, ``scanned`` the engine's own coverage line — the
    only part of the output the two engines legitimately disagree about,
    because "complete over corpus, N deep-searched" and "N targets, M lines"
    are different true statements about different work. ``filters`` is the
    query language's own spelling of the filters that applied.
    """
    out: list[str] = [f"[ctx search {ref.display()}]"]
    out.append(
        "patterns: " + " ".join(repr(p) for p in patterns) + (" (all)" if mode_all else " (any)")
        + (f" · {filters}" if filters else "")
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
    record_telemetry(store, "search", telemetry_bytes, len(result.encode("utf-8")), ref=ref_text)
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

    # -------- the query language: filters ride in the pattern list
    # (docs/CODE-SEARCH.md). Only repo searches have paths, languages,
    # symbols and history to filter on; a run:/blob: search keeps every
    # token as a pattern.
    from ctx import searchq

    query = None
    if ref.kind == "repo":
        try:
            query = searchq.parse(list(patterns))
        except searchq.QueryError as e:
            raise RetrievalError(str(e)) from e
        patterns = list(query.patterns)
        if query.kind != "code":
            return _search_history(store, ws, ref, ref_text, query, cap=cap)
        if not patterns and not query.syms:
            raise RetrievalError("at least one pattern is required (filters alone select nothing)")

    # MULTILINE keeps ^/$ line-anchored now that matching runs whole-text.
    flags = re.MULTILINE
    if query is not None and query.case is False:
        flags |= re.IGNORECASE
    try:
        rxs = [
            re.compile(re.escape(p) if fixed else p, flags)
            for p in patterns
        ]
    except re.error as e:
        raise RetrievalError(f"invalid pattern: {e}") from e

    # -------- repo searches: the text index narrows the corpus first
    # (candidates from trigrams, verified below against the live bytes), then
    # ripgrep when installed, then the Python engine. A pattern with no
    # literal run of three bytes has no trigrams and skips the index.
    index_targets = None
    if ref.kind == "repo" and query is not None:
        index_targets = _index_candidates(
            store, ws, ref, query, rxs, fixed=fixed, mode_all=mode_all, glob=glob, scope=scope
        )
        if index_targets is not None and not patterns:
            # sym:Name alone: the answer is the definition sites themselves.
            return _render_symbol_sites(store, ws, ref, ref_text, query, index_targets, cap=cap)

    if index_targets is None and ref.kind == "repo" and _rg_available() and not (
        query is not None and query.has_filters
    ):
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
    engine_note = ""
    oversized: list[str] = []
    if index_targets is not None:
        targets, considered, skipped_binary, engine_note, oversized = index_targets
    elif ref.kind == "run":
        targets, skipped_binary = _resolve_run_targets(store, ref, glob=glob)
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
            store, ws, ref, glob=glob, scope=scope,
            keep=(lambda rel: searchq.keep_path(rel, query)) if query is not None and query.has_filters else None,
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
        before, text_line, after = _hit_window(t, hit.line_start, context, ws.config.redaction)
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

    total = len(matches)
    direct_note = ""
    scanned_targets = len(targets)
    telemetry_bytes = sum(len(t.text) for t in targets)
    if oversized and considered <= DIRECT_SCAN_MAX_FILES:
        # A file named on its own (or a small set): scanned directly, its
        # real matches merged into the same rows, ordered and capped as one.
        cut_short: list[str] = []
        scanned_direct = 0
        for rel in oversized:
            if _sniff_binary(ws, rel):
                skipped_binary += 1  # as the in-memory engines count it
                continue
            try:
                d_rows, d_total, d_lines, d_read, d_size = _scan_direct(
                    ws, rel, rxs, mode_all=mode_all, context=context, cap=cap)
            except OSError:
                continue
            rows.extend(d_rows)
            total += d_total
            scanned_lines += d_lines
            scanned_targets += 1
            scanned_direct += 1
            telemetry_bytes += d_read
            if d_read < d_size:
                cut_short.append(f"{rel} (first {fmt_int(d_read)} of {fmt_int(d_size)} bytes)")
        rows.sort(key=lambda r: (r.target, r.line_no))
        del rows[cap:]
        direct_note = ((f" · {scanned_direct} over the 1 MiB index limit scanned directly"
                        if scanned_direct else "")
                       + (f"; stopped early: {', '.join(cut_short)}" if cut_short else ""))
    elif oversized:
        names = ", ".join(oversized[:5]) + (f", … {len(oversized) - 5} more"
                                            if len(oversized) > 5 else "")
        direct_note = (f"\n  {len(oversized)} files over the 1 MiB index limit were not "
                       f"searched: {names} (name one to search it: "
                       "ctx search repo:<file> PATTERN)")
    if engine_note:
        scanned = (
            f"  scanned: {fmt_int(scanned_targets)} of {fmt_int(considered)} targets · "
            f"{fmt_int(scanned_lines)} lines · {engine_note}"
            + (f" · {skipped_binary} binary skipped" if skipped_binary else "") + direct_note
        )
    else:
        scanned = (
            f"  scanned: {fmt_int(considered)} targets · {fmt_int(scanned_lines)} lines"
            + (f" · {skipped_binary} binary skipped" if skipped_binary else "")
        )
    return _render_search(
        store, ws, ref, ref_text, patterns, rows,
        mode_all=mode_all,
        total=total,
        scanned=scanned,
        cap=cap,
        # Snapshot-on-read for repo evidence (SPEC §6.3) — only repo targets
        # have a workspace file to pin.
        snapshots=ref.kind == "repo",
        telemetry_bytes=telemetry_bytes,
        filters=query.describe() if query is not None else "",
    )


def _index_candidates(
    store: Store, ws: Workspace, ref: Ref, query, rxs, *, fixed: bool, mode_all: bool,
    glob: str | None, scope: str | None,
) -> tuple[list[SearchTarget], int, int, str, list[str]] | None:
    """The text-index rung: candidate files from trigrams (and ``sym:``),
    read for verification. None when the index is off, unbuilt and too big
    to build implicitly, or when nothing in the query has a trigram to
    offer — the lower rungs then scan the corpus as before.

    Returns ``(targets, corpus size, binary skipped, engine note, files of
    the corpus over the index's size limit)``."""
    import os

    from ctx import codeindex, searchq

    if os.environ.get("CTX_SEARCH_ENGINE", "auto") not in ("auto", "index"):
        return None
    if fixed:
        exprs = [codeindex.expr_for_literal(_unescape(rx.pattern)) for rx in rxs]
    else:
        exprs = [codeindex.expr_for_regex(rx.pattern) for rx in rxs]
    expr = codeindex._and(exprs) if (mode_all or len(exprs) == 1) else codeindex._or(exprs)
    if codeindex.is_unrestricted(expr) and not query.syms and not query.has_filters:
        return None
    try:
        idx = codeindex.open_index(store, ws)
    except Exception:
        return None
    if idx is None:
        return None
    try:
        # The corpus the search is over: the subtree/scope/glob, then the
        # query's own path and language filters.
        subset = _corpus_rels(ws, ref, glob=glob, scope=scope)
        keep = [r for r in subset if searchq.keep_path(r, query)] if query.has_filters else subset
        if query.syms:
            defining = set()
            for name in query.syms:
                defining.update(rel for rel, _k, _l in idx.files_defining(name))
            keep = [r for r in keep if r in defining]
        considered = len(keep)
        rels = idx.candidates(expr, subset=keep)
        # Byte-stable on an unchanged tree: no timings, no "re-indexed"
        # counts (search results are content-addressed and cached on them).
        note = "index trigram"
        # Files the index left out for size are not candidates of anything:
        # without this a search of a 2 MB log answered "matches: 0".
        oversized = _oversized(ws, keep, idx.files)
    finally:
        idx.close()
    targets: list[SearchTarget] = []
    skipped_binary = 0
    for rel in rels:
        try:
            data = (ws.root / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            skipped_binary += 1
            continue
        targets.append(SearchTarget(label=rel, text=data.decode("utf-8", "replace")))
    return targets, considered, skipped_binary, note, oversized


#: A search whose corpus is at most this many files (a file named on its
#: own, or a small explicit set) scans its files over the index's size limit
#: directly instead of skipping them. A wider search names them instead.
DIRECT_SCAN_MAX_FILES = 8
#: Bytes a direct scan reads per file, at most; past it the scan stops and
#: says how far it got.
DIRECT_SCAN_MAX_BYTES = 256 << 20
#: Read size of one block of the direct scan (whole lines; a longer line is
#: read on until its end or the bound below).
_DIRECT_BLOCK = 1 << 20
#: A single line longer than this is cut into pieces of this size.
_DIRECT_LINE_MAX = 8 << 20
#: Text kept around a block for context lines and private-key lookbehind.
_DIRECT_EDGE_CHARS = 16384


def _oversized(ws: Workspace, rels: list[str], indexed) -> list[str]:
    """``rels`` the index left out for size (it drops files over
    ``codeindex.MAX_FILE_BYTES``): only files it does not hold are stat'ed."""
    from ctx import codeindex

    out = []
    for rel in rels:
        if rel in indexed:
            continue
        try:
            if (ws.root / rel).stat().st_size > codeindex.MAX_FILE_BYTES:
                out.append(rel)
        except OSError:
            continue
    return out


def _sniff_binary(ws: Workspace, rel: str) -> bool:
    """The binary test every search engine here applies: a NUL byte in the
    first 8 KiB."""
    try:
        with open(ws.root / rel, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return False


def _line_blocks(fh, limit: int):
    """Decoded blocks of whole lines from a binary file object, in order,
    reading at most ``limit`` bytes. Yields ``(text, nbytes)``; the last
    block may lack a trailing newline. Memory is bounded by one block plus
    one line (a line over ``_DIRECT_LINE_MAX`` is cut)."""
    carry = b""
    read = 0
    while read < limit:
        chunk = fh.read(min(_DIRECT_BLOCK, limit - read))
        if not chunk:
            break
        read += len(chunk)
        buf = carry + chunk
        cut = buf.rfind(b"\n") + 1
        if cut == 0 and len(buf) < _DIRECT_LINE_MAX:
            carry = buf
            continue
        if cut == 0:
            cut = len(buf)
        carry = buf[cut:]
        yield buf[:cut].decode("utf-8", "replace"), cut
    if carry:
        yield carry.decode("utf-8", "replace"), len(carry)


def _scan_direct(
    ws: Workspace, rel: str, rxs, *, mode_all: bool, context: int, cap: int,
) -> tuple[list[RenderRow], int, int, int, int]:
    """A bounded streaming scan of one file the index does not hold.

    Blocks of whole lines are matched with the same ``finditer`` and
    leftmost-match-per-line rule as the in-memory engine; a hit is rendered
    with :func:`_hit_window` over the block plus the text around it, so its
    lines are cut, redaction-aware and private-key-blanked the same way.
    Only the first ``cap`` hits are rendered; all are counted.

    Returns ``(rows, total, lines, bytes_read, file_bytes)``.
    """
    path = ws.root / rel
    size = path.stat().st_size
    rows: list[RenderRow] = []
    total = 0
    seen: set[int] = set()
    base = 1  # line number of the block's first line
    nread = 0
    lines = 0
    policy = ws.config.redaction
    with open(path, "rb") as fh:
        blocks = _line_blocks(fh, DIRECT_SCAN_MAX_BYTES)
        prev = ""
        cur = next(blocks, None)
        while cur is not None:
            text, nbytes = cur
            nxt = next(blocks, None)
            nread += nbytes
            last = nxt is None
            per_line: dict[int, tuple[int, int, int]] = {}
            for pi, rx in enumerate(rxs):
                line_start = 0
                next_nl = text.find("\n")
                for m in rx.finditer(text):
                    start = m.start()
                    if start >= len(text) and not last:
                        break  # the next block's first line: matched there
                    while next_nl != -1 and next_nl < start:
                        line_start = next_nl + 1
                        next_nl = text.find("\n", line_start)
                    seen.add(pi)
                    old = per_line.get(line_start)
                    if old is None or (start, pi) < (old[1], old[0]):
                        per_line[line_start] = (pi, start, m.end())
            total += len(per_line)
            if per_line and len(rows) < cap:
                pre = prev[-_DIRECT_EDGE_CHARS:]
                pre = pre[pre.find("\n") + 1:] if len(prev) > _DIRECT_EDGE_CHARS else pre
                post = ""
                if nxt is not None and context:
                    stop = -1
                    for _ in range(context):
                        stop = nxt[0].find("\n", stop + 1, _DIRECT_EDGE_CHARS)
                        if stop < 0:
                            break
                    post = nxt[0][: stop if stop >= 0 else min(len(nxt[0]), _DIRECT_EDGE_CHARS)]
                target = SearchTarget(label=rel, text=pre + text + post)
                n = base
                at = 0
                for ls in sorted(per_line):
                    if len(rows) >= cap:
                        break
                    n += text.count("\n", at, ls)
                    at = ls
                    pi, a, b = per_line[ls]
                    before, line, after = _hit_window(target, len(pre) + ls, context, policy)
                    rows.append(RenderRow(target=rel, line_no=n, col_a=a - ls + 1,
                                          col_b=b - ls + 1, text=line, before=before,
                                          after=after, contextual=bool(context)))
            nl = text.count("\n")
            base += nl
            lines += nl + (1 if last and text and not text.endswith("\n") else 0)
            prev = text
            cur = nxt
    if mode_all and len(seen) < len(rxs):
        return [], 0, lines, nread, size
    return rows, total, lines, nread, size


def _unescape(pattern: str) -> str:
    """``re.escape`` undone for the fixed-string case, so the literal's own
    trigrams are looked up rather than its backslashes'."""
    return re.sub(r"\\(.)", r"\1", pattern)


def _corpus_rels(ws: Workspace, ref: Ref, *, glob: str | None, scope: str | None) -> list[str]:
    from ctx.sessiondir import LEDGER_DIR_NAME

    from .targets import _glob_match

    if scope:
        scoped = ws.config.scopes.get(scope)
        if not scoped:
            raise RetrievalError(
                f"unknown scope {scope!r}; configured: {sorted(ws.config.scopes) or 'none'}"
            )
        roots = list(scoped)
    else:
        roots = [ref.path]
    rels: list[str] = []
    for root in roots:
        rels.extend(ws.list_files(root))
    rels = sorted(dict.fromkeys(rels))
    rels = [r for r in rels if r.replace("\\", "/").split("/")[0] != LEDGER_DIR_NAME]
    if glob:
        rels = [r for r in rels if _glob_match(r, glob)]
    return rels


def _render_symbol_sites(
    store: Store, ws: Workspace, ref: Ref, ref_text: str, query, index_targets, *, cap: int
) -> str:
    """``sym:Name`` with no pattern: the definition sites, from the index's
    symbol table, each verified against the file's current line."""
    from ctx import codeindex

    targets, considered, skipped_binary, note, _oversized_rels = index_targets
    idx = codeindex.Index(store, ws)
    rows: list[RenderRow] = []
    try:
        by_label = {t.label: t for t in targets}
        sites = []
        for name in query.syms:
            sites.extend((rel, kind, line, name) for rel, kind, line in idx.files_defining(name)
                         if rel in by_label)
        sites.sort()
        for rel, kind, line, name in sites[:cap]:
            lines = by_label[rel].text.split("\n")
            text = (clip_redacting(lines[line - 1].strip(), _LINE_CHARS, ws.config.redaction)
                    if 0 < line <= len(lines) else "")
            col = text.find(name) + 1 if name in text else 0
            rows.append(RenderRow(target=rel, line_no=line, col_a=col, col_b=col + len(name) if col else 0,
                                  text=text, before=[], after=[], contextual=False))
    finally:
        idx.close()
    return _render_search(
        store, ws, ref, ref_text, [f"sym:{s}" for s in query.syms], rows,
        mode_all=False, total=len(sites), cap=cap,
        scanned=f"  scanned: {fmt_int(len(targets))} of {fmt_int(considered)} targets · symbol table · {note}",
        snapshots=True, telemetry_bytes=sum(len(t.text) for t in targets),
        filters=query.describe(),
    )


def _search_history(store: Store, ws: Workspace, ref: Ref, ref_text: str, query, *, cap: int) -> str:
    """``type:commit`` / ``type:diff``: commits as evidence rows. The
    history renderer is the query language's own (:mod:`ctx.searchq`); a
    commit is not a hit and does not wear a hit's format."""
    from ctx import searchq

    try:
        rows = searchq.history(ws, query, max_count=max(cap, 1))
    except searchq.QueryError as e:
        raise RetrievalError(str(e)) from e
    except OSError as e:
        raise RetrievalError(f"git: {e}") from e
    payload = {"query": query.describe(), "patterns": query.patterns, "rows": [r.payload() for r in rows]}
    blob = short_id(store.put_blob(canonical_json(payload)))
    text = searchq.render_history(ref.display(), query, rows, cap=cap, blob=blob)
    result = _emit(ws, text, ws.config.budgets.result_tokens, None, handle=ref_text)
    record_telemetry(store, "search", sum(len(r.subject) for r in rows), len(result.encode("utf-8")))
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
            before, text_line, after = _hit_window(t, starts[m.target][m.line_no], context,
                                                   ws.config.redaction)
            contextual = True
        else:
            # No context asked for, or the file could not be re-read (or is
            # empty): fall back to rg's own copy of the matched line, unmarked.
            before, text_line, after = (), clip_redacting(m.line, _LINE_CHARS, ws.config.redaction), ()
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
