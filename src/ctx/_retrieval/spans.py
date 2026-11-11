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
from ctx.textutil import EVIDENCE_LINE_CHARS, clip_redacting, fmt_int, index_lines
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
    else:  # pragma: no cover - registry only writes these kinds
        raise RetrievalError(f"unknown span kind {span['kind']!r}")

    return _emit(ws, "\n".join(out), budget.result_tokens)


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
