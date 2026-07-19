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
from ctx.textutil import fmt_int
from ctx.workspace import Workspace

from .common import RetrievalError, _emit


def _resolve_span(store: Store, ws: Workspace, ref_text: str, label: str, span_id: str) -> str:
    budget = ws.config.budgets
    span = store.get_span(span_id)
    blob = span["blob"]
    out = [f"[ctx get {label} span:{span['span_id']}]"]

    if span["kind"] == "region":
        a, b = int(span["a"]), int(span["b"])
        total = b - a + 1
        if total <= budget.max_inline_lines:
            chunk = store.read_blob_lines(blob, a, b)
            lines = chunk.decode("utf-8", "replace").splitlines()
            out.append(f"region: L{a}:{b} ({fmt_int(total)} lines, complete)")
            out.extend(f"L{a + i}: {ln}" for i, ln in enumerate(lines))
        else:
            out.append(f"region: L{a}:{b} ({fmt_int(total)} lines) — zoom digest")
            out.extend(_zoom_region(store, blob, a, b, budget.max_inline_lines))
    elif span["kind"] == "template":
        template = span["template"] or ""
        from ctx.digest.logprof import mask_line

        data = store.get_blob(blob)
        all_lines = data.decode("utf-8", "replace").splitlines()
        occurrences = [
            (i, raw)
            for i, raw in enumerate(all_lines, start=1)
            if raw.strip() and mask_line(raw) == template
        ]
        cap = 20
        out.append(f"template: {template[:160]}")
        out.append(f"occurrences (exact): {fmt_int(len(occurrences))} · shown: {min(cap, len(occurrences))}")
        for i, raw in occurrences[:cap]:
            out.append(f"L{i}: {raw[:180]}")
        if len(occurrences) > cap:
            first_hidden = occurrences[cap][0]
            out.append(
                f"… +{fmt_int(len(occurrences) - cap)} more · "
                f"next: ctx get {ref_text} --lines {first_hidden}:{min(first_hidden + budget.max_inline_lines - 1, len(all_lines))}"
            )
    else:  # pragma: no cover - registry only writes the two kinds
        raise RetrievalError(f"unknown span kind {span['kind']!r}")

    return _emit(ws, "\n".join(out), budget.result_tokens)


def _zoom_region(store: Store, blob: str, a: int, b: int, inline_cap: int) -> list[str]:
    """Bounded zoom into a large region: template-mine the slice, surface
    exceptional lines, and mint sub-spans for recursive descent."""
    from ctx.digest.logprof import mine_templates

    chunk = store.read_blob_lines(blob, a, b)
    lines = chunk.decode("utf-8", "replace").splitlines()
    templates, mined = mine_templates(lines, first_line_no=a)

    out: list[str] = []
    ranked = sorted(templates.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))
    out.append(f"templates: {fmt_int(len(templates))} over {fmt_int(len(mined))} lines")
    for tpl, (count, first) in ranked[:8]:
        line = f"  {fmt_int(count)}× L{first}: {tpl[:150]}"
        if count > 1:
            sid = store.register_span(blob, "template", template=tpl)
            line += f" · span {sid}"
        out.append(line)

    rare = [(i, raw) for i, tpl, raw in mined if templates[tpl][0] <= 2]
    if rare:
        out.append("exceptional:")
        for i, raw in rare[:10]:
            out.append(f"  L{i}: {raw[:170]}")
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
