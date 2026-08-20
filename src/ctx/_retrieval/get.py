"""``ctx get``: exact bounded slice with provenance (SPEC §6.4). Oversized
requests return a bounded preview plus continuation coordinates — never
silent flooding."""

from __future__ import annotations

from ctx import anchors, bounds

import json
from dataclasses import dataclass
from typing import Any

from ctx.execution import snapshot_file
from ctx.store import Store
from ctx.textutil import decode_exact, encode_exact, fmt_int, short_id
from ctx.workspace import Workspace

from .common import RetrievalError, _emit, _parse, _peek_blob, _read_bytes_range, _route_workspace
from .spans import _resolve_span
from .telemetry import record_telemetry


@dataclass(frozen=True, slots=True)
class Selector:
    lines: tuple[int, int] | None = None
    bytes: tuple[int, int] | None = None
    records: tuple[int, int] | None = None
    json_pointer: str | None = None
    symbol: str | None = None  # Python: dotted def/class name via stdlib ast
    span: str | None = None  # opaque span token minted by a digest (SPEC §6.4)
    #: Content the caller asserts is at ``lines`` (see ``ctx.anchors``). Set
    #: only by an anchored ``--lines A:B@anchor``; verification, relocation and
    #: refusal all hang off it being present.
    lines_anchor: str | None = None
    #: Render ``L40:a3| text`` instead of ``L40: text`` — per-line content tags
    #: the model can name individual lines by when it goes on to edit them.
    hashlines: bool = False


def _window(flag: str, a: int, b: int, total: int, unit: str) -> tuple[int, int]:
    """Clamp a range selector into the content, or refuse it. One shape.

    Every ``ctx get`` range selector makes the same promise: the header says
    ``--<flag> A:B of N`` and the body is what that range covers. A start past
    the end breaks it in the quietest possible way -- an empty body, exit 0,
    and a header stating a range whose start exceeds its own total.

    ``--lines`` was hardened for this twice, once per code path, and the
    hardening stopped there; the next bug bash walked in through ``--records``,
    which had the identical hole. Three selectors, one contract, so one
    function: a new selector inherits the refusal instead of re-earning it.
    """
    window = bounds.span(a, b, total)
    if window is None:
        suggest = min(total, 200) or 1
        raise RetrievalError(
            f"--{flag} {a}:{b} selects nothing: the content has "
            f"{fmt_int(total)} {unit}. Use --{flag} 1:{suggest} or omit "
            f"--{flag}."
        )
    return window


#: Room left for the two header lines so the body is what the header claims.
_HEADER_RESERVE = 256


def _resolve_anchor(
    all_lines: list[str], a: int, b: int, want: str, ref_text: str
) -> tuple[int, int, str | None]:
    """Resolve an anchored ``--lines A:B@anchor`` against the file as it is now.

    Three outcomes, and the caller can tell them apart:

      * unchanged -- the anchored content is still at ``a:b``; returns the range
        untouched with no note, so a verified read looks exactly like the read
        that minted the address. Declaring omissions is house style; narrating
        successes is window tax.
      * moved -- the content is elsewhere in the file; returns its current range
        and a note naming the move. The caller asked for *content*, and the
        content is what comes back.
      * gone -- raises. The alternative is to return whatever now occupies lines
        ``a:b``, which is what an unanchored address already does and is the
        entire defect this grammar exists to close. A read that cannot keep its
        promise fails; it does not quietly answer a different question.
    """
    span_len = b - a + 1
    window = all_lines[a - 1 : b] if 1 <= a <= len(all_lines) else []
    if len(window) == span_len and anchors.anchor(window) == want:
        return a, b, None
    moved = anchors.relocate(all_lines, want, span_len, a)
    if moved is None:
        raise RetrievalError(
            f"anchor @{want} not found in {ref_text}: the content that was at "
            f"lines {a}:{b} is no longer in this file (it was edited or "
            f"deleted). Re-navigate — `ctx def`/`ctx refs`/`ctx search` mint a "
            f"fresh anchored address — or read the current lines with "
            f"`ctx get {ref_text} --lines {a}:{b}` if the coordinates are what "
            f"you meant."
        )
    end = moved + span_len - 1
    return moved, end, f"anchor: @{want} moved L{a}:{b} → L{moved}:{end} (content unchanged)"


def _byte_window(ref_text, a: int, b: int, total: int, budget):
    """Clamp a byte range to the inline budget and address the remainder.

    `--lines` has always clamped to max_inline_lines and pointed at the next
    window; `--bytes` did not, so an oversized request went to the generic
    result-budget backstop instead -- which cuts at a line boundary, and on
    the newline-sparse content --bytes exists to serve that cut landed on the
    HEADER's own newline and deleted the whole payload. Worse, the fallback
    address dropped the selector, so the continuation offering to reach the
    rest re-read the stream as lines.

    Clamping here means the cut is the SELECTOR's, so the continuation can
    name the next window and actually advance.
    """
    # The TIGHTER of the two budgets that apply. max_inline_bytes bounds the
    # selector; the result-token budget bounds the emission, and it is
    # usually smaller. Clamping to only the first left the header saying
    # "--bytes 1:16384" over a body that bounded() had since cut to ~4.7 KB
    # -- the header describing a range the body does not contain, which is
    # the same lie in a smaller font.
    room = bounds.budget_bytes(getattr(budget, "result_tokens", 0)) - _HEADER_RESERVE
    cap = min(
        bounds.count(getattr(budget, "max_inline_bytes", 0)) or (b - a + 1),
        room if room > 0 else (b - a + 1),
    )
    if b - a + 1 <= cap:
        nxt = f"ctx get {ref_text} --bytes {b + 1}:{min(total, b + (b - a + 1))}"
        return a, b, (nxt if b < total else None)
    b = a + cap - 1
    return a, b, f"ctx get {ref_text} --bytes {b + 1}:{min(total, b + cap)}"


def _fit_window(
    flag: str, ref_text, a: int, b: int, total: int, rendered: list[str], budget,
    addr=None,
):
    """Trim a 1-based item window to what the result budget can actually hold.

    `--lines` clamped to max_inline_lines and stopped there, but 240 lines of
    a wide file still exceed the token budget -- so bounded() cut it again,
    and THAT cut belongs to nobody: the header still claimed A:B and the
    fallback continuation re-issued the same range, sending the reader back
    to a line already shown. `--bytes` learned this in round 12; `--lines`
    is the same lesson, and the selector has to own the cut for the
    continuation to advance.

    `flag` rather than a hardcoded `--lines`, because `--records` was the
    third door onto this same contract and was still open: a `--records A:B`
    covering the final record with a body over budget had no selector-level
    continuation at all, so bounded() fell back to the verbatim handle and
    the emitted `next:` re-issued the identical range -- the same truncated
    prefix, forever. One fitter for every item-window selector is what stops
    a fourth one being written; `tests/test_selector_continuation.py`
    enumerates them so a new selector fails until it is wired in here.

    Measured on the RENDERED body rather than estimated from a width, which
    is the only way the header, the body and the address agree exactly.

    `addr` spells the continuation's selector value. A continuation that
    dropped the anchor would hand back a *weaker* address than the one that
    produced it, so following the chain would silently shed verification at
    the first budget cut -- the same class of defect as dropping the selector
    itself, which is what this helper was written for.
    """
    room = bounds.budget_bytes(getattr(budget, "result_tokens", 0)) - _HEADER_RESERVE
    if room <= 0:
        return b, None
    kept, used = 0, 0
    for line in rendered:
        used += len(encode_exact(line)) + 1
        if used > room:
            break
        kept += 1
    if kept >= len(rendered):
        return b, None
    kept = max(1, kept)
    # Reached only when the window was actually trimmed (`kept < len(rendered)`
    # above), so `new_b < b <= total`: there is always a next item to address
    # and the continuation strictly advances. That is the invariant the whole
    # helper exists to hold, and it is why the trimming branch may never
    # return None -- a cut with no forward address is the loop itself.
    new_b = a + kept - 1
    span = new_b - a + 1
    nxt_a, nxt_b = new_b + 1, min(total, new_b + span)
    value = addr(nxt_a, nxt_b) if addr else f"{nxt_a}:{nxt_b}"
    return new_b, f"ctx get {ref_text} {flag} {value}"


def get(
    store: Store,
    ws: Workspace,
    ref_text: str,
    selector: Selector,
) -> str:
    """Exact bounded slice with provenance. Oversized requests return a
    bounded preview plus continuation coordinates (never silent flooding)."""
    ref = _parse(ref_text)
    store, ws = _route_workspace(store, ws, ref)
    budget = ws.config.budgets

    label: str
    data: bytes
    divergence: str | None = None

    # Whether the bytes behind this address can change under it. Only repo:
    # names a live worktree file; run:, blob: and snapshot: name content the
    # store has frozen. Anchors are minted for the mutable kind alone -- an
    # address that cannot go stale does not need a staleness check, and paying
    # for one on every immutable line span would be pure window tax.
    mutable = ref.kind == "repo"

    fast_lines: tuple[str, int] | None = None  # (blob_hash, total_lines)
    fast_lines_raw = 0
    fast_bytes: tuple[str, int] | None = None  # (blob_hash, total_bytes)

    # A selector combination that will end up needing the FULL decoded blob
    # regardless of ref kind: json-pointer (JSON parse) and records (must
    # scan every line to index non-blank records). Line-index/byte-range
    # fast paths below are only ever eligible when neither is requested —
    # this mirrors the priority the render dispatch below already uses
    # (json_pointer > records > bytes > lines), so no selector combination
    # changes behavior, only which path avoids a redundant full read.
    #
    # An anchored --lines joins them: verifying content that is where it was
    # needs only the window, but RELOCATING content that moved needs the whole
    # file to search. Deciding per outcome would make the fast path's
    # eligibility depend on a result it has not computed yet, so an anchor
    # simply opts out of seeking. It costs a full read on the one selector
    # that asked to be checked, and keeps verification uniform across every
    # ref kind rather than strong on some addresses and absent on others.
    needs_full_scan = (
        selector.json_pointer is not None
        or selector.records is not None
        or selector.lines_anchor is not None
    )

    if ref.kind == "run":
        manifest = store.get_manifest(ref.id or "")
        short = short_id(manifest["id"])
        stream = ref.stream or "stdout"
        meta = manifest["streams"].get(stream)
        if meta is None:
            raise RetrievalError(f"run:{short} has no stream {stream!r}")
        blob_hash = str(meta["blob"]).removeprefix("sha256:")
        label = f"run:{short}#{stream}"
        is_octet_stream = str(meta["mediaType"]).startswith("application/octet-stream")
        if selector.span is not None:
            data = b""  # span resolution reads only what it needs
        elif not needs_full_scan and selector.bytes is not None:
            # Byte-range selector: seek instead of loading the whole stream.
            fast_bytes = (blob_hash, int(meta["bytes"]))
            data = b""
        elif not needs_full_scan and not is_octet_stream:
            # Line-index fast path: touch only the requested byte range.
            # Covers both an explicit --lines and the implicit default view
            # (head lines) — both end up in the same "lines" render branch
            # below, so both are eligible.
            fast_lines = (blob_hash, int(meta["lines"]))
            fast_lines_raw = int(meta["bytes"])
            data = b""
        else:
            data = store.get_blob(blob_hash)
    elif ref.kind in ("blob", "snapshot"):
        if ref.kind == "snapshot":
            manifest = store.get_manifest(ref.id or "")
            data = store.get_blob(str(manifest["blob"]).removeprefix("sha256:"))
            label = f"snapshot:{short_id(manifest['id'])} ({manifest.get('path', '?')})"
            # Label divergence when the current worktree differs (SPEC §15).
            # (Needs the full blob either way, to diff byte-for-byte against
            # the live file — no fast path applies here.)
            try:
                current = ws.confine(str(manifest["path"])).read_bytes()
                if current != data:
                    divergence = "current worktree file differs from this snapshot"
            except Exception:
                divergence = "file no longer present in worktree"
        else:
            blob_id = store.resolve_id(ref.id or "", kinds=("blob",))
            label = f"blob:{short_id(blob_id)}"
            if selector.span is not None:
                data = b""
            elif not needs_full_scan and selector.bytes is not None:
                fast_bytes = (blob_id, store.blob_path(blob_id).stat().st_size)
                data = b""
            elif (
                not needs_full_scan
                and selector.symbol is None
                and b"\x00" not in _peek_blob(store, blob_id)
            ):
                idx = store.line_index(blob_id)
                fast_lines = (blob_id, max(0, len(idx) - 1))
                fast_lines_raw = int(idx[-1]) if idx else 0
                data = b""
            else:
                data = store.get_blob(blob_id)
    elif ref.kind == "repo":
        if not ref.path:
            raise RetrievalError("get repo: requires a file path (repo:<path>)")
        snap = snapshot_file(store, ws, ref.path)
        data = store.get_blob(str(snap["blob"]).removeprefix("sha256:"))
        label = f"repo:{ref.path} (snapshot:{short_id(snap['id'])})"
    else:
        raise RetrievalError(f"cannot get reference kind {ref.kind!r}")

    header = [f"[ctx get {label}]"]
    body: str
    continuation: str | None = None
    lines_handle: str | None = None

    if selector.span is not None:
        result = _resolve_span(store, ws, ref_text, label, selector.span)
        record_telemetry(store, "get", len(data) if data else 0, len(encode_exact(result)))
        return result

    if selector.symbol is not None:
        # --symbol REPLACES the line range, so an anchor supplied alongside it
        # describes a range that is about to be discarded. Ignoring it would
        # leave the caller holding an address that looks verified and is not --
        # the precise failure anchors exist to remove, reintroduced by a
        # selector combination rather than by an edit.
        if selector.lines_anchor is not None:
            raise RetrievalError(
                "--symbol resolves its own line range, so it cannot be combined "
                f"with an anchored --lines A:B@{selector.lines_anchor}. Use one "
                "or the other: --symbol to look the range up now, or the "
                "anchored range to verify a range you already hold."
            )
        if fast_lines is not None:
            data = store.get_blob(fast_lines[0])
            fast_lines = None
        if fast_bytes is not None:
            data = store.get_blob(fast_bytes[0])
            fast_bytes = None
        span = _python_symbol_span(data.decode("utf-8", "replace"), selector.symbol)
        if span is None:
            raise RetrievalError(
                f"symbol {selector.symbol!r} not found (Python ast parser; "
                "for other languages use ctx search + --lines)"
            )
        header.append(f"symbol: {selector.symbol} → lines {span[0]}:{span[1]}")
        # Rebuilt as a line selector, but RENDER settings survive the rewrite:
        # dropping `hashlines` here would make `--symbol foo --hashlines` a
        # silently ignored flag. `lines_anchor` deliberately does not survive --
        # the anchor described the caller's own range, and the range has just
        # been replaced by one the symbol resolver chose.
        selector = Selector(lines=span, hashlines=selector.hashlines)

    if selector.json_pointer is not None:
        try:
            from ctx.textutil import loads_fast

            doc = loads_fast(data.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise RetrievalError(f"content is not JSON: {e}") from e
        # RFC 6901 via the one implementation (ctx.textutil.json_pointer).
        # This used to short-circuit `pointer in ("", "/")` to the whole
        # document — but "/" is the member with the EMPTY-STRING key, not
        # the root — and `lstrip("/")` collapsed "//a" to "/a".
        from ctx.textutil import JsonPointerError, json_pointer

        pointer = selector.json_pointer
        try:
            node: Any = json_pointer(doc, pointer)
        except JsonPointerError as e:
            raise RetrievalError(f"json-pointer not found: {pointer} ({e})") from None
        header.append(f"selector: --json-pointer {pointer or '/'}")
        body = json.dumps(node, indent=2, sort_keys=True, ensure_ascii=False)
    elif selector.records is not None:
        a, b = selector.records
        lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
        # Third door onto the contract _window now owns: a start past the
        # record count returned an empty body under a header claiming the
        # range, and exited 0.
        a, b = _window("records", a, b, len(lines), "records")
        rendered = lines[a - 1 : b]
        # Fit BEFORE the header is written, so the header reports the range
        # actually shown. Writing it first is how `--lines` originally came
        # to claim a range wider than its own body.
        b, fit_next = _fit_window("--records", ref_text, a, b, len(lines), rendered, budget)
        rendered = rendered[: b - a + 1]
        if fit_next:
            continuation = fit_next
        elif b < len(lines):
            continuation = f"ctx get {ref_text} --records {b + 1}:{min(len(lines), b + (b - a + 1))}"
        header.append(f"selector: --records {a}:{b} of {fmt_int(len(lines))}")
        body = "\n".join(rendered)
    elif selector.bytes is not None:
        a, b = selector.bytes
        if fast_bytes is not None:
            blob_hash_b, total_b = fast_bytes
            a, b = _window("bytes", a, b, total_b, "bytes")
            a, b, continuation = _byte_window(ref_text, a, b, total_b, budget)
            chunk = _read_bytes_range(store, blob_hash_b, a, b)
            header.append(f"selector: --bytes {a}:{b} of {fmt_int(total_b)}")
            # decode_exact, not errors="replace": --bytes is THE exact-bytes
            # escape hatch (it is what this module tells callers to use for
            # binary content), and "replace" turned every undecodable byte
            # into a 3-byte U+FFFD -- neither the same bytes nor even the same
            # LENGTH, through the tool's own exactness interface.
            body = decode_exact(chunk)
        else:
            a, b = _window("bytes", a, b, len(data), "bytes")
            a, b, continuation = _byte_window(ref_text, a, b, len(data), budget)
            chunk = data[a - 1 : b]
            header.append(f"selector: --bytes {a}:{b} of {fmt_int(len(data))}")
            body = decode_exact(chunk)  # second door onto the same contract
    else:
        if fast_lines is not None:
            blob_hash, total = fast_lines
            a, b = selector.lines if selector.lines is not None else (1, total)
            if selector.lines is None:
                b = min(total, ws.config.budgets.max_inline_lines)
            # bounds.span, not a lone `min(b, total)`: only the END was
            # clamped, so a START past EOF printed a self-contradictory
            # header (start > total) over a silently empty body and still
            # exited 0. An empty span is empty, and says so.
            a, b = _window("lines", a, b, total, "lines")
            if b - a + 1 > budget.max_inline_lines:
                b = a + budget.max_inline_lines - 1
                continuation = f"ctx get {ref_text} --lines {b + 1}:{min(total, b + budget.max_inline_lines)}"
            chunk = store.read_blob_lines(blob_hash, a, b)
            all_lines = chunk.decode("utf-8", "replace").splitlines()
            rendered = anchors.render_window(all_lines, a, tagged=selector.hashlines)
            b, fit_next = _fit_window("--lines", ref_text, a, b, total, rendered, budget)
            if fit_next:
                continuation = fit_next
                rendered = rendered[: b - a + 1]
            header.append(f"selector: --lines {a}:{b} of {fmt_int(total)}")
            body = "\n".join(rendered)
        else:
            if b"\x00" in data[:8192]:
                raise RetrievalError("binary content: use --bytes A:B for exact slices")
            all_lines = data.decode("utf-8", "replace").splitlines()
            if selector.lines is None:
                a, b = 1, min(len(all_lines), ws.config.budgets.max_inline_lines)
            else:
                a, b = selector.lines
            # Anchor resolution runs BEFORE the range is clamped. An edit that
            # deleted lines can leave an anchored address pointing past the new
            # end of file, and _window refuses a start past the end -- so
            # clamping first would answer "that range selects nothing" about
            # content that is still in the file, three lines up. The address
            # names content; where that content currently sits is the answer,
            # not a precondition for looking.
            if selector.lines_anchor is not None:
                a, b, moved = _resolve_anchor(
                    all_lines, a, b, selector.lines_anchor, ref_text
                )
                if moved:
                    header.append(moved)
            # Second door onto the same contract: this path clamped only the
            # END too, so `--lines 1000:5` on a 5-line file printed the header
            # "--lines 1000:5 of 5" -- a range whose start exceeds its own
            # total -- over an empty body, and exited 0.
            a, b = _window("lines", a, b, len(all_lines), "lines")
            # How this ref spells a --lines value. A mutable target (repo:)
            # mints the anchor of the window being addressed, so every address
            # this call emits -- the selector it echoes and any continuation it
            # offers -- is verifiable when it comes back. Immutable targets
            # mint nothing: their bytes cannot move, and the nine characters
            # would buy a guarantee the ref kind already gives for free.
            def _addr(x: int, y: int) -> str:
                if not mutable:
                    return f"{x}:{y}"
                return anchors.format_span(x, y, anchors.anchor(all_lines[x - 1 : y]))

            if b - a + 1 > budget.max_inline_lines:
                b = a + budget.max_inline_lines - 1
                nxt_b = min(len(all_lines), b + budget.max_inline_lines)
                continuation = f"ctx get {ref_text} --lines {_addr(b + 1, nxt_b)}"
            rendered = anchors.render_window(
                all_lines[a - 1 : b], a, tagged=selector.hashlines
            )
            b, fit_next = _fit_window(
                "--lines", ref_text, a, b, len(all_lines), rendered, budget, addr=_addr
            )
            if fit_next:
                continuation = fit_next
                rendered = rendered[: b - a + 1]
            header.append(
                f"selector: --lines {_addr(a, b)} of {fmt_int(len(all_lines))}"
            )
            # The budget-truncation fallback address, resolved. `selector.lines`
            # holds what the CALLER asked for, which after a relocation is the
            # range the content has just been shown to have left -- so the one
            # address emitted when the token budget cuts the body would send the
            # reader back to coordinates this very call reported as stale.
            lines_handle = f"{ref_text} --lines {_addr(a, b)}"
            body = "\n".join(rendered)

    if divergence:
        header.append(f"divergence: {divergence}")
    # The budget-truncation fallback address must carry the SELECTOR, not
    # just the ref. Without it an over-budget `--bytes A:B` emitted
    # "next: ctx get run:<id>#stdout" -- an address that re-reads the whole
    # stream as lines, so the bytes the caller asked for became unreachable
    # through the very continuation offering to reach them.
    handle = ref_text
    if lines_handle is not None:
        handle = lines_handle
    elif selector.bytes is not None:
        handle = f"{ref_text} --bytes {selector.bytes[0]}:{selector.bytes[1]}"
    elif selector.records is not None:
        handle = f"{ref_text} --records {selector.records[0]}:{selector.records[1]}"
    elif selector.lines is not None:
        # --lines was the one selector this fix skipped when it was written,
        # so an over-budget `--lines A:B` emitted "next: ctx get <ref>" --
        # an address that re-reads the stream from line 1. The continuation
        # offering to reach the rest sent the reader back to the start.
        handle = f"{ref_text} --lines {selector.lines[0]}:{selector.lines[1]}"
    result = _emit(ws, "\n".join(header) + "\n" + body, budget.result_tokens, continuation,
                   handle=handle, exact=selector.bytes is not None)
    if fast_lines is not None:
        raw_len = fast_lines_raw
    elif fast_bytes is not None:
        raw_len = fast_bytes[1]
    else:
        raw_len = len(data)
    record_telemetry(store, "get", raw_len, len(encode_exact(result)))
    return result


def _python_symbol_span(source: str, dotted: str) -> tuple[int, int] | None:
    """Line span of a def/class by dotted-name suffix using stdlib ast.
    Matches ``func``, ``Class.method``, or ``module-level path`` suffixes."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    want = dotted.split(".")

    def walk(node: Any, path: list[str]) -> tuple[int, int] | None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_path = path + [child.name]
                if new_path[-len(want) :] == want:
                    end = getattr(child, "end_lineno", child.lineno)
                    # Include decorators in the span.
                    start = min(
                        [child.lineno] + [d.lineno for d in child.decorator_list]
                    )
                    return (start, end)
                found = walk(child, new_path)
                if found:
                    return found
        return None

    return walk(tree, [])
