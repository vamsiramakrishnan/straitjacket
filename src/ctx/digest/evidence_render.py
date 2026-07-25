"""Plan-obeying evidence renderer for census-grade profiles (EDC §12, §14).

Layer 4 of the EDC stack: a PURE function from (EvidenceGraph, contract,
DeliveryPlan) to RenderedEvidence — no signals, no state, no clock, no
store. Rule 14 verbatim: same evidence + contract + plan → identical
bytes (tests/test_evidence_core.py enforces it as a property test).

Doctrine encoded:

* **Identities never drop outside FLOOD.** The degradation cascade under
  budget pressure is teaching prose first, then the root-detail body,
  then one-liner compression — and when even the bare identity census
  cannot fit, the render ESCALATES to FLOOD with declared partial inline
  coverage and a content-addressed full-census blob. It never silently
  truncates identities (EDC §13).
* **Addresses are contract-driven, not plan-driven** (EDC §12 correction
  1): every census row carries its stdout coordinates / span token in
  every mode; ``include_addresses`` governs teaching prose only.
* **Group labels are extracted keys** (EDC §12 correction 2): DENSE
  grouping labels are the items' file or failure-class keys, never
  invented topic prose.
* **Derived artifacts are minted blob: handles** (EDC §12 correction 3):
  FLOOD's machine-readable census is canonical JSON, content-addressed;
  this module computes the id purely (sha256 of the canonical bytes) and
  the impure caller persists it via ``store.put_blob`` — the two are
  identical by construction.
* **Validation at the selection seam** (EDC §5.3 amendment 1): coverage
  receipts come from :func:`ctx.contracts.validate_selection` over typed
  facts, never from re-parsing rendered text.

The resolver owns the DeliveryPlan type; this module duck-types it
(mode / census / item_summary / inline_detail_count / include_teaching /
token_budget) and provides a local default with the same field names for
plan-less callers — no import edge into ctx.resolver.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any

from ctx.contracts import EvidenceContract, validate_selection
from ctx.evidence import (
    CoverageReceipt,
    EvidenceGraph,
    EvidenceItem,
    RenderedEvidence,
    graph_id,
)
from ctx.store import canonical_json
from ctx.textutil import fmt_bytes, fmt_int, short_id

CENSUS_BLOB_SCHEMA = "ctx.pytest-census/v1"

# The fact classes this renderer always delivers inline for failures.
_BASE_FIELDS = frozenset(
    {"aggregate_counts", "complete_identity_census", "location", "failure_class"}
)

_COUNT_ORDER = ("failed", "error", "skipped", "xfailed", "xpassed")
_TOTAL_KEYS = ("passed", "failed", "skipped", "error", "xfailed", "xpassed")


@dataclass(frozen=True)
class DefaultPlan:
    """Duck-type of ctx.resolver.DeliveryPlan for plan-less callers.
    Field names match the resolver's frozen dataclass exactly."""

    mode: str
    census: str = "complete"
    item_summary: str = "one_line"
    inline_detail_count: int = 1
    include_addresses: bool = True
    include_teaching: bool = True
    token_budget: int = 2400
    evidence_floor: int = 0
    hard_ceiling: int = 1_000_000
    reasons: tuple = ()


def default_fail_plan(budgets: Any, *, dense: bool = False) -> DefaultPlan:
    """The plan a plan-less failure render obeys: mirrors the pre-EDC
    budget behavior (result budget × failure factor) plus the mechanism-E
    anticipatory gate (root-detail body only at result_tokens >= 600)."""
    result_tokens = int(getattr(budgets, "result_tokens", 1200))
    factor = float(getattr(budgets, "failure_budget_factor", 2.0))
    return DefaultPlan(
        mode="dense" if dense else "fail_census",
        item_summary="expanded" if dense else "one_line",
        inline_detail_count=1 if result_tokens >= 600 else 0,
        token_budget=int(result_tokens * factor),
    )


@dataclass(frozen=True)
class RenderEnv:
    """Deterministic run-context the renderer may not invent: stream line
    counts (coverage tail), pre-formatted stderr/focus evidence lines, and
    the byte envelope the digest wrapper will add around the body. All
    values are pure derivatives of the captured bytes — Rule 14 holds over
    (graph, contract, plan, env)."""

    stdout_lines: int = 0
    stderr_lines: int = 0
    parsed_fully: bool = True
    parse_cap_note: str = ""
    stderr_head: str | None = None
    focus_lines: tuple[str, ...] = ()
    envelope_bytes: int = 0
    run_ref: str = "run:PENDING"


# ------------------------------------------------------------------ helpers
def _nbytes(lines: list[str]) -> int:
    return sum(len(ln.encode("utf-8")) + 1 for ln in lines)


def _short_nodeid(nid: str) -> str:
    if "::" not in nid:
        return nid
    path, rest = nid.split("::", 1)
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 2:
        path = "/".join(parts[-2:])
    return path + "::" + rest


def _item_coords(item: EvidenceItem) -> tuple[int | None, int | None, str | None]:
    a = item.attributes.get("stdout_a")
    b = item.attributes.get("stdout_b")
    sid = None
    if item.detail_ref is not None and item.detail_ref.selector:
        sel = item.detail_ref.selector
        if sel.startswith("span:"):
            sid = sel.removeprefix("span:")
    return (int(a) if a is not None else None, int(b) if b is not None else None, sid)


def _addr(item: EvidenceItem) -> str:
    a, b, sid = _item_coords(item)
    if a is None:
        return ""
    if b is not None:
        out = f"stdout:L{a}-L{b}"
    else:
        out = f"stdout:L{a}"
    if sid:
        out += f" · span {sid}"
    return out


def _file_key(item: EvidenceItem) -> str:
    if item.location and ":" in item.location:
        return item.location.rsplit(":", 1)[0]
    if item.location:
        return item.location
    if "::" in item.id:
        return item.id.split("::", 1)[0]
    return "?"


def _row_lines(idx: int, item: EvidenceItem, summaries: str, indent: str) -> list[str]:
    row = f"{indent}{idx}. {_short_nodeid(item.id)[:160]}  {item.location or '?'} · {item.failure_class or '?'}"
    if summaries in ("one_line", "expanded") and item.summary:
        row += f" · {item.summary[:120]}"
    addr = _addr(item)
    if addr:
        row += f" · {addr}"
    lines = [row]
    if summaries == "expanded":
        for ev in tuple(item.attributes.get("evidence_lines", ()))[:2]:
            lines.append(indent + "    " + str(ev)[:160])
    return lines


def _summary_lines(graph: EvidenceGraph) -> list[str]:
    counts = {k: v for k, v in graph.aggregate.items() if isinstance(v, int)}
    lines = ["summary:"]
    if counts:
        total = sum(v for k, v in counts.items() if k in _TOTAL_KEYS)
        parts = [f"passed {fmt_int(counts.get('passed', 0))}"]
        for key in _COUNT_ORDER:
            if counts.get(key):
                parts.append(f"{key} {fmt_int(counts[key])}")
        lines.append(f"  tests: {fmt_int(total)} · " + " · ".join(parts))
    else:
        lines.append("  tests: summary line not found (session may have crashed)")
    return lines


def _denominator(graph: EvidenceGraph) -> int:
    return max(len(graph.items), int(graph.coverage.get("total_estimate", 0)))


def _root_item(graph: EvidenceGraph) -> EvidenceItem | None:
    for item in graph.items:
        if item.attributes.get("stdout_b") is not None:
            return item
    return None


def _pointer_line(root: EvidenceItem) -> str:
    a, b, sid = _item_coords(root)
    name = str(root.attributes.get("block_name") or root.id.split("::")[-1])
    line = f"  first failure stdout:L{a}-L{b}: {name}"
    if sid:
        line += f" · span {sid}"
    return line


def _teaching_lines(graph: EvidenceGraph, env: RenderEnv) -> list[str]:
    suggestions: list[str] = []
    first = graph.items[0] if graph.items else None
    if first is not None:
        probe_src = first.summary or ""
        words = re.sub(r"[^\w.-]", " ", probe_src).split()
        probe = words[0] if words else first.id.split("::")[-1]
        suggestions.append(f"ctx search {env.run_ref} '{probe}' 'FAILED' --context 3")
    root = _root_item(graph)
    if root is not None:
        a, b, _ = _item_coords(root)
        suggestions.append(f"ctx get {env.run_ref}#stdout --lines {a}:{b}")
    if not suggestions:
        suggestions.append(f"ctx search {env.run_ref} 'failed' 'error' --context 3")
    return ["next:"] + [f"  {s}" for s in suggestions[:3]]


def _coverage_tail(env: RenderEnv, shown_spans: int) -> list[str]:
    total = env.stdout_lines + env.stderr_lines
    lines = []
    if env.parsed_fully:
        lines.append(f"  parsed: {fmt_int(total)}/{fmt_int(total)} lines")
    else:
        cap = env.parse_cap_note or fmt_bytes(32 * 1024 * 1024)
        lines.append(f"  parsed: partial (streams exceed {cap}) of {fmt_int(total)} lines")
    omitted = max(0, total - shown_spans)
    lines.append(f"  shown: {shown_spans} spans · omitted: {fmt_int(omitted)} lines")
    return lines


def _attested_marker(graph: EvidenceGraph) -> str:
    if bool(graph.coverage.get("complete")):
        return "attested complete"
    return "completeness unattested (output truncated?)"


# ---------------------------------------------------------- flood blob mint
def flood_census_payload(graph: EvidenceGraph) -> bytes:
    """Canonical-JSON bytes of the complete machine-readable census (EDC
    §12 correction 3). Content-addressed; the caller persists via
    ``store.put_blob`` and the hash is identical by construction."""
    items = []
    for item in graph.items:
        a, b, sid = _item_coords(item)
        items.append(
            {
                "id": item.id,
                "location": item.location,
                "failure_class": item.failure_class,
                "summary": item.summary,
                "stdout_lines": [a, b],
                "selector": item.detail_ref.selector if item.detail_ref else None,
            }
        )
    return canonical_json(
        {
            "schema": CENSUS_BLOB_SCHEMA,
            "family": graph.family,
            "graph": graph_id(graph),
            "outcome": graph.outcome,
            "aggregate": {k: v for k, v in graph.aggregate.items()},
            "coverage": dict(graph.coverage),
            "items": items,
        }
    )


def flood_census_blob_id(graph: EvidenceGraph) -> str:
    return hashlib.sha256(flood_census_payload(graph)).hexdigest()


# ------------------------------------------------------------- composition
def _receipt(
    graph: EvidenceGraph,
    contract: EvidenceContract,
    selected_ids: list[str],
    included_fields: frozenset[str],
) -> CoverageReceipt:
    return validate_selection(selected_ids, included_fields, contract, graph)


def _compose(
    graph: EvidenceGraph,
    contract: EvidenceContract,
    env: RenderEnv,
    mode: str,
    *,
    teaching: bool,
    detail_body: bool,
    summaries: str,
) -> tuple[list[str], list[str], frozenset[str]]:
    """One deterministic rendering at one degradation rung. Returns
    (lines, selected_ids, included_fields)."""
    items = graph.items
    denom = _denominator(graph)
    marker = {"fail_census": "", "dense": "dense · ", "bypass": "bypass · "}.get(mode, "")
    head = f"[pytest/v2 · {graph.outcome} · {marker}coverage={len(items)}/{denom}]"
    lines: list[str] = [head]
    lines += _summary_lines(graph)

    # ---- census (identities never drop at this layer)
    if mode == "dense":
        files = {_file_key(i) for i in items}
        dim = "file" if len(files) > 1 else "class"
        keyf = _file_key if dim == "file" else (lambda i: i.failure_class or "?")
        lines.append(f"failing tests (census · by {dim}):")
        groups: dict[str, list[tuple[int, EvidenceItem]]] = {}
        order: list[str] = []
        for idx, item in enumerate(items, start=1):
            key = keyf(item)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((idx, item))
        for key in order:
            lines.append(f"  {key} ({len(groups[key])}):")
            for idx, item in groups[key]:
                lines += _row_lines(idx, item, summaries, "    ")
    else:
        lines.append("failing tests (census):")
        for idx, item in enumerate(items, start=1):
            lines += _row_lines(idx, item, summaries, "  ")

    # ---- root detail: address-bearing pointer always; body per plan rung
    root = _root_item(graph)
    shown = len(items)
    included = set(_BASE_FIELDS)
    if summaries in ("one_line", "expanded"):
        included.add("one_line_summary")
    if root is not None:
        lines.append(_pointer_line(root))
        included.add("root_detail")
        shown += 1
        if detail_body:
            for raw in tuple(root.attributes.get("detail_head", ())):
                lines.append(f"    | {str(raw)[:160]}")

    # ---- run-context evidence (stderr head, focus spans)
    if env.stderr_head:
        lines.append(f"  stderr head stderr:L1: {env.stderr_head[:140]}")
        shown += 1
    for focus_line in env.focus_lines:
        lines.append(focus_line)
        shown += 1

    # ---- coverage block
    lines.append("coverage:")
    lines.append(
        f"  census: {len(items)}/{denom} identities inline · {_attested_marker(graph)}"
    )
    lines += _coverage_tail(env, shown)

    if teaching:
        lines += _teaching_lines(graph, env)
    return lines, [i.id for i in items], frozenset(included)


def _degradation_steps(plan: Any) -> list[tuple[bool, bool, str]]:
    """The committed degradation order (EDC §19): teaching drops first,
    then the root-detail body, then one-liner compression. Identities and
    addresses are not in the cascade — they escalate to FLOOD instead."""
    teaching0 = bool(getattr(plan, "include_teaching", True))
    body0 = int(getattr(plan, "inline_detail_count", 1)) >= 1
    s0 = str(getattr(plan, "item_summary", "one_line"))
    if s0 not in ("expanded", "one_line"):
        s0 = "class_only"
    ladders = {
        "expanded": ["expanded", "one_line", "class_only"],
        "one_line": ["one_line", "class_only"],
        "class_only": ["class_only"],
    }
    steps: list[tuple[bool, bool, str]] = [(teaching0, body0, s0)]
    if teaching0:
        steps.append((False, body0, s0))
    if body0:
        steps.append((False, False, s0))
    for s in ladders[s0][1:]:
        steps.append((False, False, s))
    seen: set[tuple[bool, bool, str]] = set()
    out = []
    for step in steps:
        if step not in seen:
            seen.add(step)
            out.append(step)
    return out


def _render_flood(
    graph: EvidenceGraph,
    contract: EvidenceContract,
    plan: Any,
    env: RenderEnv,
    budget_bytes: int,
) -> RenderedEvidence:
    """FLOOD (EDC §12.5): class+file histograms, a first-N stable census
    prefix in occurrence order, declared omissions, and the complete
    census as a content-addressed canonical-JSON blob reference."""
    items = graph.items
    denom = _denominator(graph)
    blob12 = short_id(flood_census_blob_id(graph))

    hist_class: dict[str, int] = {}
    hist_file: dict[str, int] = {}
    for item in items:
        hist_class[item.failure_class or "?"] = hist_class.get(item.failure_class or "?", 0) + 1
        hist_file[_file_key(item)] = hist_file.get(_file_key(item), 0) + 1

    def build(k: int) -> tuple[list[str], list[str], frozenset[str]]:
        lines = [f"[pytest/v2 · {graph.outcome} · flood · coverage={k}/{denom}]"]
        lines += _summary_lines(graph)
        lines.append("failure classes:")
        for name, n in sorted(hist_class.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {name}  {n}")
        lines.append("files:")
        for name, n in sorted(hist_file.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {name}  {n}")
        lines.append(f"census (first {k} of {denom}, occurrence order):")
        for idx, item in enumerate(items[:k], start=1):
            lines += _row_lines(idx, item, "class_only", "  ")
        if k < len(items):
            lines.append(
                f"  … +{len(items) - k} more identities · full census blob:{blob12}"
            )
        lines.append("coverage:")
        lines.append(
            f"  census: {k}/{denom} identities inline · {_attested_marker(graph)}"
            f" · full census blob:{blob12}"
        )
        lines += _coverage_tail(env, k)
        return lines, [i.id for i in items[:k]], frozenset(_BASE_FIELDS)

    chosen = build(1)
    for k in range(len(items), 0, -1):
        candidate = build(k)
        if _nbytes(candidate[0]) <= budget_bytes:
            chosen = candidate
            break
    lines, selected, included = chosen
    receipt = _receipt(graph, contract, selected, included)
    receipt = replace(
        receipt,
        omitted_bytes=max(0, _nbytes(build(len(items))[0]) - _nbytes(lines)),
    )
    return RenderedEvidence("\n".join(lines), receipt, plan)


# ------------------------------------------------------------------ entry
def render_fail_evidence(
    graph: EvidenceGraph,
    contract: EvidenceContract,
    plan: Any,
    env: RenderEnv | None = None,
) -> RenderedEvidence:
    """The plan-obeying renderer (EDC §14): pure function of its arguments,
    returns text + the selection-seam CoverageReceipt + the plan.

    A render that cannot fit the identity census within the plan budget
    escalates to FLOOD (declared partial inline coverage + full-census
    blob) — it never truncates identities (EDC §13)."""
    env = env or RenderEnv()
    budget_bytes = max(0, int(getattr(plan, "token_budget", 0)) * 4 - env.envelope_bytes)
    mode = str(getattr(plan, "mode", "fail_census"))
    if mode == "flood" or str(getattr(plan, "census", "complete")) == "bounded":
        return _render_flood(graph, contract, plan, env, budget_bytes)
    if mode not in ("fail_census", "dense", "bypass"):
        mode = "fail_census"
    for teaching, detail_body, summaries in _degradation_steps(plan):
        lines, selected, included = _compose(
            graph,
            contract,
            env,
            mode,
            teaching=teaching,
            detail_body=detail_body,
            summaries=summaries,
        )
        if _nbytes(lines) <= budget_bytes:
            receipt = _receipt(graph, contract, selected, included)
            return RenderedEvidence("\n".join(lines), receipt, plan)
    # Even the bare identity census does not fit: escalate, never truncate.
    return _render_flood(graph, contract, plan, env, budget_bytes)


__all__ = [
    "CENSUS_BLOB_SCHEMA",
    "DefaultPlan",
    "RenderEnv",
    "default_fail_plan",
    "flood_census_payload",
    "flood_census_blob_id",
    "render_fail_evidence",
]
