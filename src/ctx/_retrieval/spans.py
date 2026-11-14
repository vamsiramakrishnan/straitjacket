"""Zoom spans: resolving a digest-minted span token to a bounded view
(SPEC §6.4).

The invariant that beats raw-refill retrieval: resolution can never flood.
Small regions return exact lines; large regions return a zoom sub-digest
(template mining over just that slice) minting fresh sub-spans; template
spans return a bounded occurrence listing. Every level declares its
omission and hands out coordinates.
"""

from __future__ import annotations

from ctx.store import Store
from ctx.textutil import EVIDENCE_LINE_CHARS, clip_redacting, fmt_int, index_lines, redaction_scope
from ctx.workspace import Workspace

from .common import RetrievalError, _emit, _read_bytes_range, blob_reader, edge_masked_bytes


def _context_redacted(store: Store, blob: str, start: int, end: int, policy) -> str:
    """Bytes ``[start, end)`` of a blob with secrets crossing the span's
    edges blanked at the same width (see ``common.edge_masked_bytes``): a
    span's edges are chosen by ctx (a chunk-map piece boundary, a region
    boundary inside a private-key block)."""
    total = store.blob_path(store.resolve_id(blob, kinds=("blob",))).stat().st_size
    return edge_masked_bytes(blob_reader(store, blob), start, end, total, policy)


def _resolve_span(store: Store, ws: Workspace, ref_text: str, label: str, span_id: str) -> str:
    budget = ws.config.budgets
    span = store.get_span(span_id)
    blob = span["blob"]
    out = [f"[ctx get {label} span:{span['span_id']}]"]

    if span["kind"] == "region":
        a, b = int(span["a"]), int(span["b"])
        total = b - a + 1
        if total <= budget.max_inline_lines:
            idx = store.line_index(blob)
            n_lines = max(0, len(idx) - 1)
            if 1 <= a <= b <= n_lines:
                text = _context_redacted(store, blob, int(idx[a - 1]), int(idx[b]),
                                         ws.config.redaction)
            else:
                text = store.read_blob_lines(blob, a, b).decode("utf-8", "replace")
            lines = index_lines(text)
            out.append(f"region: L{a}:{b} ({fmt_int(total)} lines, complete)")
            out.extend(f"L{a + i}: {ln}" for i, ln in enumerate(lines))
        else:
            out.append(f"region: L{a}:{b} ({fmt_int(total)} lines) — zoom digest")
            out.extend(_zoom_region(store, blob, a, b, budget.max_inline_lines))
    elif span["kind"] == "template":
        template = span["template"] or ""
        from ctx.digest.logprof import mask_line

        data = store.get_blob(blob)
        all_lines = index_lines(data.decode("utf-8", "replace"))
        occurrences = [
            (i, raw)
            for i, raw in enumerate(all_lines, start=1)
            if raw.strip() and mask_line(raw) == template
        ]
        cap = 20
        from ctx.digest.logprof import display_template

        from ctx.textutil import PRIVATE_KEY_LINE, in_line_ranges, private_key_line_ranges

        keys = private_key_line_ranges("\n".join(all_lines), policy=ws.config.redaction)
        shown_tpl = (display_template(occurrences[0][1], EVIDENCE_LINE_CHARS,
                                      in_key_block=in_line_ranges(occurrences[0][0], keys))
                     if occurrences else clip_redacting(template, EVIDENCE_LINE_CHARS))
        out.append(f"template: {shown_tpl}")
        out.append(f"occurrences (exact): {fmt_int(len(occurrences))} · shown: {min(cap, len(occurrences))}")
        for i, raw in occurrences[:cap]:
            shown = PRIVATE_KEY_LINE if in_line_ranges(i, keys) else raw
            out.append(f"L{i}: {clip_redacting(shown, 180, ws.config.redaction)}")
        if len(occurrences) > cap:
            first_hidden = occurrences[cap][0]
            out.append(
                f"… +{fmt_int(len(occurrences) - cap)} more · "
                f"next: ctx get {ref_text} --lines {first_hidden}:{min(first_hidden + budget.max_inline_lines - 1, len(all_lines))}"
            )
    elif span["kind"] == "bytes":
        # A byte-range span: a piece of a line too long to address by line
        # number (minified JSON, base64), minted by the intake chunk map.
        # The mint bounds the range to one retrieval's worth; the emission
        # budget below still backstops it.
        from .common import _read_bytes_range

        a, b = int(span["a"]), int(span["b"])
        chunk = _read_bytes_range(store, blob, a, b)
        out.append(f"bytes: {a}:{b} ({fmt_int(len(chunk))} bytes, complete)")
        # The piece's edges were chosen by ctx: a secret crossing them is
        # masked here, judged with the bytes around the piece.
        out.append(_context_redacted(store, blob, a - 1, a - 1 + len(chunk), ws.config.redaction))
    elif span["kind"] == "messages":
        out.extend(_message_listing(store, ws, ref_text, blob, int(span["a"]), int(span["b"])))
    elif span["kind"] == "message_lines":
        out.extend(_message_lines(store, ws, ref_text, blob, str(span["template"] or "{}")))
    elif span["kind"] == "messages_from":
        # a = first line to map, b = first row of the page
        out.extend(_message_listing(store, ws, ref_text, blob, int(span["b"]), 10**9,
                                    start_line=int(span["a"])))
    else:  # pragma: no cover - registry only writes these kinds
        raise RetrievalError(f"unknown span kind {span['kind']!r}")

    return _emit(ws, "\n".join(out), budget.result_tokens)


#: Rows one page of a ``messages`` span lists before handing out the next page.
MESSAGE_PAGE_ROWS = 40


def _message_listing(store: Store, ws: Workspace, ref_text: str, blob: str,
                     a: int, b: int, *, start_line: int = 1) -> list[str]:
    """Every distinct message of one stream (the evidence map's grouping),
    ordered by first occurrence, rows ``a``..``b`` of that order, a page at a
    time. ``start_line`` > 1 maps the stream from that line on (the part a
    digest's parse cap left out). Exact for the lines it says it read."""
    from ctx.digest.base import MAX_PARSE_BYTES
    from ctx.digest.evidence_map import LIST_SAMPLE_CHARS, _shown_line, _varies, build_streams, line_of

    path = store.blob_path(store.resolve_id(blob, kinds=("blob",)))
    offset = 0
    if start_line > 1:
        idx = store.line_index(blob)
        if start_line - 1 >= len(idx):
            raise RetrievalError(f"line {start_line} is past the end of {ref_text}")
        offset = int(idx[start_line - 1])
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(MAX_PARSE_BYTES + 1)
    whole = len(data) <= MAX_PARSE_BYTES
    text = data[:MAX_PARSE_BYTES].decode("utf-8", "replace")
    total_lines = len(store.line_index(blob)) - 1 if not whole else 0
    with redaction_scope(ws.config.redaction):
        emap = build_streams([("stdout", text, whole, 0)], first_line=start_line)
    msgs = sorted(emap.messages, key=lambda m: m.first) if emap else []
    total = len(msgs)
    a = max(1, a)
    end = min(b, total, a + MESSAGE_PAGE_ROWS - 1)
    last_read = start_line + (len(emap.lines["stdout"]) if emap else text.count("\n")) - 1
    scope = f"lines {start_line}-{last_read}"
    if not whole:
        scope += f" of {fmt_int(total_lines)}; later lines are not in this listing"
    out = [f"messages (exact for {scope}): {fmt_int(total)} distinct · "
           f"{fmt_int(emap.signal_lines if emap else 0)} error-like lines · "
           f"rows {a}-{end} by first occurrence (×count · lines · first sample)"]
    fired: set[str] = set()
    with redaction_scope(ws.config.redaction):
        for m in msgs[a - 1:end]:
            span = (f"first L{m.first}  last L{m.last}" if m.count > 1 else f"L{m.first}")
            sample = _shown_line(line_of(emap, "stdout", m.first).strip(), LIST_SAMPLE_CHARS, fired)
            out.append(f"  ×{fmt_int(m.count)}  {span}  {sample}{_varies(m, fired)}")
    if end < min(b, total):
        if start_line > 1:
            sid = store.register_span(blob, "messages_from", a=start_line, b=end + 1)
        else:
            sid = store.register_span(blob, "messages", a=end + 1, b=total)
        out.append(f"… +{fmt_int(min(b, total) - end)} more · next: ctx get {ref_text} --span {sid}")
    if not whole:
        sid = store.register_span(blob, "messages_from", a=last_read + 1, b=1)
        out.append(f"next part: ctx get {ref_text} --span {sid}")
    return out


def _message_lines(store: Store, ws: Workspace, ref_text: str, blob: str, spec_text: str) -> list[str]:
    """Every line of one evidence-map message (same skeleton, same split
    values), ``MESSAGE_PAGE_ROWS`` at a time from row ``from``."""
    import json

    from ctx.digest.base import MAX_PARSE_BYTES
    from ctx.digest.evidence_map import (
        EXCERPT_LINE_CHARS, _shown_line, signal_rows, skeleton, strip_ansi)

    spec = json.loads(spec_text)
    sk, fixed = spec.get("sk", ""), {int(k): v for k, v in (spec.get("fixed") or {}).items()}
    start = max(1, int(spec.get("from", 1)))
    path = store.blob_path(store.resolve_id(blob, kinds=("blob",)))
    with path.open("rb") as fh:
        data = fh.read(MAX_PARSE_BYTES + 1)
    whole = len(data) <= MAX_PARSE_BYTES
    text = strip_ansi(data[:MAX_PARSE_BYTES].decode("utf-8", "replace"))
    lines = index_lines(text)
    hits = []
    for n, _tier in signal_rows(text, lines):
        got, vals = skeleton(lines[n - 1])
        if got == sk and all(j < len(vals) and vals[j] == v for j, v in fixed.items()):
            hits.append(n)
    page = hits[start - 1:start - 1 + MESSAGE_PAGE_ROWS]
    scope = "" if whole else " in the parsed part"
    out = [f"message lines (exact{scope}): {fmt_int(len(hits))} · rows {start}-"
           f"{start + len(page) - 1}"]
    fired: set[str] = set()
    from ctx.textutil import PRIVATE_KEY_LINE, in_line_ranges, private_key_line_ranges

    with redaction_scope(ws.config.redaction):
        keys = private_key_line_ranges(text)
        for n in page:
            shown = PRIVATE_KEY_LINE if in_line_ranges(n, keys) else lines[n - 1]
            out.append(f"L{n}: {_shown_line(shown, EXCERPT_LINE_CHARS, fired)}")
    if start - 1 + len(page) < len(hits):
        nxt = dict(spec, **{"from": start + len(page)})
        sid = store.register_span(blob, "message_lines", template=json.dumps(nxt, sort_keys=True))
        out.append(f"… +{fmt_int(len(hits) - start + 1 - len(page))} more · next: ctx get {ref_text} --span {sid}")
    return out


def _zoom_region(store: Store, blob: str, a: int, b: int, inline_cap: int) -> list[str]:
    """Bounded zoom into a large region: template-mine the slice, surface
    exceptional lines, and mint sub-spans for recursive descent."""
    from ctx.digest.logprof import display_template, mine_templates

    chunk = store.read_blob_lines(blob, a, b)
    lines = index_lines(chunk.decode("utf-8", "replace"))
    templates, mined = mine_templates(lines, first_line_no=a)
    # A region can start inside a private-key block: locate blocks with the
    # text before it (bounded) so its body lines are masked by block.
    from ctx.textutil import PEM_LOOKBEHIND_CHARS, PRIVATE_KEY_LINE, in_line_ranges, private_key_line_ranges

    start_byte = int(store.line_index(blob)[a - 1])
    before = _read_bytes_range(store, blob, max(1, start_byte - 4 * PEM_LOOKBEHIND_CHARS + 1),
                               start_byte).decode("utf-8", "replace") if start_byte else ""
    key_ranges = [(x + a - 1, y + a - 1) for x, y in
                  private_key_line_ranges("\n".join(lines) + "\n", before)]

    out: list[str] = []
    ranked = sorted(templates.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))
    out.append(f"templates: {fmt_int(len(templates))} over {fmt_int(len(mined))} lines")
    for tpl, (count, first) in ranked[:8]:
        line = (f"  {fmt_int(count)}× L{first}: "
                f"{display_template(lines[first - a], 150, in_key_block=in_line_ranges(first, key_ranges))}")
        if count > 1:
            sid = store.register_span(blob, "template", template=tpl)
            line += f" · span {sid}"
        out.append(line)

    rare = [(i, raw) for i, tpl, raw in mined if templates[tpl][0] <= 2]
    if rare:
        out.append("exceptional:")
        for i, raw in rare[:10]:
            out.append(f"  L{i}: {clip_redacting(PRIVATE_KEY_LINE if in_line_ranges(i, key_ranges) else raw, 170)}")
        if len(rare) > 10:
            out.append(f"  … +{fmt_int(len(rare) - 10)} more exceptional lines")

    # Halving sub-spans give log-depth descent to any coordinate.
    mid = a + (b - a) // 2
    if b - a + 1 > inline_cap and mid > a:
        left = store.register_span(blob, "region", a=a, b=mid)
        right = store.register_span(blob, "region", a=mid + 1, b=b)
        out.append(
            f"zoom: L{a}:{mid} · span {left}  |  L{mid + 1}:{b} · span {right}"
        )
    return out
