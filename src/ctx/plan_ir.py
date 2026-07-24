"""``ctx.plan/v1`` — the compiled evidence-plan IR (docs/EVIDENCE-PLANS.md).

A plan is a model-authored, TOTAL, bounded DAG of logical evidence
operations. Totality is by construction, exactly like ``ctx q``'s 8-stage
cap, generalized to a DAG:

- **no loops, no recursion**: every ``input``/``after`` edge must reference
  an EARLIER step, so cycles are unrepresentable and execution order is the
  plan's own step order (deterministic by construction);
- **bounded fan-out**: ``foreach`` requires an explicit ``cap`` at or below
  the configured ceiling; overflow executes as declared omission, never
  silently;
- **guards, not expressions**: ``when`` admits only a micro-grammar over
  upstream results (``<node>.count > 0``, ``<node>.outcome == fail``) —
  computed control flow stays in ``ctx py``, off the MCP tier;
- **static cost**: every op carries a cost class, so ``ctx plan price``
  renders the bill before anything executes (the PRICED-CONTEXT idiom).

Plans are JSON (stdlib-parsed; canonical-JSON identity; stored as ``blob:``
and cited in the digest header, like ``ctx py`` scripts). Never YAML —
it would be the core's first hard dependency.

Validation failures are typed ``Rejection`` records with reasons drawn
from a closed vocabulary (ledger-shaped — free text cannot train epoch
tables). All checks are static: a plan that validates cannot fail for a
structural reason at execution time.

Naming discipline: this object is always the *evidence plan*; the
resolver's ``DeliveryPlan`` keeps its name unqualified.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ctx.store import canonical_json

PLAN_SCHEMA = "ctx.plan/v1"

# Hard ceilings (never raised by config; ctx.toml [plan] may only tighten).
MAX_NODES_HARD = 24
MAX_FANOUT_HARD = 64

ON_ERROR_CHOICES = ("skip_dependents", "fail")
ON_MISSING_CHOICES = ("degrade", "skip", "fail")

#: Closed rejection vocabulary (typed, ledger-shaped).
REJECTION_VOCABULARY = (
    "bad_schema",
    "bad_objective",
    "bad_budget",
    "bad_id",
    "duplicate_id",
    "unknown_op",
    "forward_reference",  # input/after names a later or unknown step ⇒ subsumes cycles
    "input_required",
    "source_takes_no_input",
    "kind_mismatch",
    "node_budget_exceeded",
    "fanout_uncapped",
    "fanout_cap_exceeded",
    "guard_grammar",
    "bad_args",
    "bad_on_error",
    "bad_on_missing",
    "execute_on_observe_tier",
    "engine_unavailable",
    "bad_requires",
)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_WHEN_COUNT_RE = re.compile(r"^([a-z][a-z0-9_]{0,31})\.count\s*(==|!=|>=|<=|>|<)\s*(\d+)$")
_WHEN_OUTCOME_RE = re.compile(r"^([a-z][a-z0-9_]{0,31})\.outcome\s*(==|!=)\s*(pass|fail)$")


class PlanError(Exception):
    """Unparseable plan document (JSON or shape). Validation problems are
    returned as typed Rejections, never raised."""


@dataclass(frozen=True)
class Rejection:
    """One typed validation failure. ``reason`` is closed-vocabulary;
    ``detail`` is the human teaching line (never parsed by machines)."""

    reason: str
    node: str | None
    detail: str

    def __post_init__(self) -> None:
        if self.reason not in REJECTION_VOCABULARY:
            raise ValueError(f"rejection reason outside the closed vocabulary: {self.reason!r}")

    def render(self) -> str:
        where = f" [{self.node}]" if self.node else ""
        return f"{self.reason}{where}: {self.detail}"


@dataclass(frozen=True)
class PlanStep:
    id: str
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    input: str | None = None  # data-flow edge (single upstream id)
    after: tuple[str, ...] = ()  # ordering-only edges
    foreach: str | None = None  # row field of `input` to iterate
    cap: int | None = None  # mandatory with foreach
    when: str | None = None  # guard micro-grammar
    on_error: str = "skip_dependents"
    on_missing: str | None = None  # None ⇒ the op's declared default

    def upstream(self) -> tuple[str, ...]:
        ids = list(self.after)
        if self.input is not None:
            ids.append(self.input)
        return tuple(dict.fromkeys(ids))


@dataclass(frozen=True)
class EvidencePlan:
    version: str
    objective_kind: str
    question: str
    budget: dict[str, Any]
    steps: tuple[PlanStep, ...]
    rank_by: tuple[str, ...]
    sections: tuple[str, ...]
    raw: dict[str, Any]  # the parsed document, for canonical storage
    # Optional required evidence floors (plan_value.EVIDENCE_DIMENSIONS):
    # tuple of {"dimension": str, "floor": float}. Additive — old plans
    # parse with () and validate exactly as before; conservative defaults
    # then derive from objective_kind (plan_value.required_floors).
    requires: tuple[dict[str, Any], ...] = ()

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.raw)

    def plan_id(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def step(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


RANK_KEYS = (
    "dynamic_confirmation",
    "changedness",
    "causal_proximity",
    "semantic_confidence",
)
DEFAULT_RANK_BY = ("dynamic_confirmation", "changedness", "causal_proximity")
DEFAULT_SECTIONS = ("conclusion_candidates", "counterevidence", "coverage")


# ------------------------------------------------------------------ parsing
def parse_plan(text_or_doc: str | dict[str, Any]) -> EvidencePlan:
    """Parse a plan document. Raises :class:`PlanError` on JSON/shape
    problems; anything semantic is left to :func:`validate_plan` so the
    caller gets ALL problems at once, typed."""
    if isinstance(text_or_doc, dict):
        doc = text_or_doc
    else:
        try:
            doc = json.loads(text_or_doc)
        except json.JSONDecodeError as e:
            raise PlanError(f"plan is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise PlanError(f"plan must be a JSON object, got {type(doc).__name__}")
    version = str(doc.get("version") or "")
    objective = doc.get("objective") or {}
    if not isinstance(objective, dict):
        raise PlanError("plan.objective must be an object")
    budget = doc.get("budget") or {}
    if not isinstance(budget, dict):
        raise PlanError("plan.budget must be an object")
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError("plan.steps must be a non-empty list")
    steps: list[PlanStep] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise PlanError(f"plan.steps[{i}] must be an object")
        after = raw.get("after") or []
        if isinstance(after, str):
            after = [after]
        if not isinstance(after, list):
            raise PlanError(f"plan.steps[{i}].after must be a list of step ids")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise PlanError(f"plan.steps[{i}].args must be an object")
        cap = raw.get("cap")
        steps.append(
            PlanStep(
                id=str(raw.get("id") or ""),
                op=str(raw.get("op") or ""),
                args=args,
                input=(str(raw["input"]) if raw.get("input") is not None else None),
                after=tuple(str(a) for a in after),
                foreach=(str(raw["foreach"]) if raw.get("foreach") is not None else None),
                cap=(int(cap) if cap is not None else None),
                when=(str(raw["when"]) if raw.get("when") is not None else None),
                on_error=str(raw.get("on_error") or "skip_dependents"),
                on_missing=(
                    str(raw["on_missing"]) if raw.get("on_missing") is not None else None
                ),
            )
        )
    emit = doc.get("emit") or {}
    if not isinstance(emit, dict):
        raise PlanError("plan.emit must be an object")
    rank_by = emit.get("rank_by") or list(DEFAULT_RANK_BY)
    sections = emit.get("sections") or list(DEFAULT_SECTIONS)
    raw_requires = objective.get("requires") or []
    requires: tuple[dict[str, Any], ...] = ()
    if isinstance(raw_requires, list):
        requires = tuple(r for r in raw_requires if isinstance(r, dict))
    return EvidencePlan(
        version=version,
        objective_kind=str(objective.get("kind") or "diagnose"),
        question=str(objective.get("question") or ""),
        budget=dict(budget),
        steps=tuple(steps),
        rank_by=tuple(str(k) for k in rank_by),
        sections=tuple(str(s) for s in sections),
        raw=doc,
        requires=requires,
    )


# --------------------------------------------------------------- validation
def _budget_int(budget: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(budget.get(key, default))
    except (TypeError, ValueError):
        return -1


def validate_plan(
    plan: EvidencePlan,
    *,
    tier: str = "cli",
    plan_policy: Any = None,
    registry: dict[str, Any] | None = None,
) -> list[Rejection]:
    """Static validation: the totality proof. Returns ALL problems as typed
    rejections (empty ⇒ the plan may execute). Nothing here runs an op.

    ``tier``: ``cli`` (full) or ``mcp`` (observe-class ops only, SPEC
    §10.4 — command execution stays on the host's visible permission
    flow). ``registry`` defaults to the shipped op registry.
    """
    from ctx import plan_ops  # late import: op registry is heavier than IR

    ops = registry if registry is not None else plan_ops.OPS
    rejections: list[Rejection] = []

    if plan.version != PLAN_SCHEMA:
        rejections.append(
            Rejection("bad_schema", None, f"version must be {PLAN_SCHEMA!r}, got {plan.version!r}")
        )
    if not plan.question:
        rejections.append(
            Rejection("bad_objective", None, "objective.question must be a non-empty string")
        )

    # Optional required evidence floors (additive; a plan without them
    # validates exactly as before this field existed).
    if plan.requires:
        from ctx.plan_value import EVIDENCE_DIMENSIONS

        for row in plan.requires:
            dim = str(row.get("dimension") or "")
            if dim not in EVIDENCE_DIMENSIONS:
                rejections.append(
                    Rejection(
                        "bad_requires", None,
                        f"unknown evidence dimension {dim!r}; closed vocabulary: "
                        + ", ".join(EVIDENCE_DIMENSIONS),
                    )
                )
            try:
                floor = float(row.get("floor", 0.0))
            except (TypeError, ValueError):
                floor = -1.0
            if not (0.0 <= floor <= 1.0):
                rejections.append(
                    Rejection(
                        "bad_requires", None,
                        f"requires floor for {dim!r} must be a number in [0, 1]",
                    )
                )

    max_nodes = MAX_NODES_HARD
    max_fanout = MAX_FANOUT_HARD
    if plan_policy is not None:
        max_nodes = min(max_nodes, int(getattr(plan_policy, "max_nodes", max_nodes)))
        max_fanout = min(max_fanout, int(getattr(plan_policy, "max_fanout", max_fanout)))
    own_nodes = _budget_int(plan.budget, "max_nodes", max_nodes)
    own_fanout = _budget_int(plan.budget, "max_fanout", max_fanout)
    if own_nodes < 1 or own_fanout < 1:
        rejections.append(
            Rejection("bad_budget", None, "budget.max_nodes/max_fanout must be positive integers")
        )
    else:
        max_nodes = min(max_nodes, own_nodes)
        max_fanout = min(max_fanout, own_fanout)
    wall = plan.budget.get("wall_seconds", None)
    if wall is not None:
        try:
            if float(wall) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            rejections.append(
                Rejection("bad_budget", None, "budget.wall_seconds must be a positive number")
            )
    tokens = plan.budget.get("max_digest_tokens", None)
    if tokens is not None and (_budget_int(plan.budget, "max_digest_tokens", 0) < 64):
        rejections.append(
            Rejection("bad_budget", None, "budget.max_digest_tokens must be an integer ≥ 64")
        )

    if len(plan.steps) > max_nodes:
        rejections.append(
            Rejection(
                "node_budget_exceeded",
                None,
                f"{len(plan.steps)} steps > max {max_nodes} (totality bound; split into epochs)",
            )
        )

    seen: dict[str, str] = {}  # id -> output kind of earlier steps
    for step in plan.steps:
        nid = step.id
        if not _ID_RE.match(nid or ""):
            rejections.append(
                Rejection("bad_id", nid or "?", "step ids match ^[a-z][a-z0-9_]{0,31}$")
            )
            continue
        if nid in seen:
            rejections.append(Rejection("duplicate_id", nid, "step id already used"))
            continue

        spec = ops.get(step.op)
        if spec is None:
            known = ", ".join(sorted(ops))
            rejections.append(
                Rejection("unknown_op", nid, f"unknown op {step.op!r}; known: {known}")
            )
            seen[nid] = "records"  # keep validating downstream shape
            continue

        # Edges: earlier-only (forward references subsume cycles).
        for up in step.upstream():
            if up not in seen:
                rejections.append(
                    Rejection(
                        "forward_reference",
                        nid,
                        f"references {up!r} which is not an earlier step "
                        "(edges point backward only; cycles are unrepresentable)",
                    )
                )

        # Kind chain (the ctx q check, generalized to a DAG). A source op
        # accepts an input ONLY as a foreach feed: the upstream rows drive
        # the iteration; the op itself stays source-shaped per item.
        out_kind = spec.output_kind
        if not spec.input_kinds:
            if step.input is not None and step.foreach is None:
                rejections.append(
                    Rejection(
                        "source_takes_no_input",
                        nid,
                        f"{step.op} is a source op (an input is only legal with foreach)",
                    )
                )
        else:
            if step.input is None:
                if not getattr(spec, "input_optional", False):
                    kinds = "|".join(spec.input_kinds)
                    rejections.append(
                        Rejection(
                            "input_required", nid, f"{step.op} needs an upstream {kinds} input"
                        )
                    )
            elif step.input in seen and seen[step.input] not in spec.input_kinds:
                kinds = "|".join(spec.input_kinds)
                rejections.append(
                    Rejection(
                        "kind_mismatch",
                        nid,
                        f"{step.op} needs {kinds}, got {seen[step.input]} from {step.input!r}",
                    )
                )
        # q.pipe: the pipeline's own kind chain is statically checkable too.
        if step.op == "q.pipe":
            from ctx.query import QueryError, parse_query, STAGES

            try:
                parsed = parse_query(str(step.args.get("query") or ""))
                kind = None
                for name, _a in parsed:
                    st = STAGES[name]
                    kind = st.output_kind if st.output_kind != "same" else kind
                out_kind = kind or "records"
            except QueryError as e:
                rejections.append(Rejection("bad_args", nid, str(e)))

        # foreach: bounded fan-out, statically.
        if step.foreach is not None:
            if step.input is None:
                rejections.append(
                    Rejection("input_required", nid, "foreach requires an input step")
                )
            if step.cap is None:
                rejections.append(
                    Rejection(
                        "fanout_uncapped",
                        nid,
                        f"foreach requires an explicit cap (≤ {max_fanout})",
                    )
                )
            elif not (1 <= step.cap <= max_fanout):
                rejections.append(
                    Rejection(
                        "fanout_cap_exceeded",
                        nid,
                        f"cap {step.cap} outside 1..{max_fanout}",
                    )
                )

        # Guard micro-grammar.
        if step.when is not None:
            m = _WHEN_COUNT_RE.match(step.when) or _WHEN_OUTCOME_RE.match(step.when)
            if not m:
                rejections.append(
                    Rejection(
                        "guard_grammar",
                        nid,
                        f"when {step.when!r} not in the guard grammar "
                        "('<node>.count <op> <int>' | '<node>.outcome ==|!= pass|fail')",
                    )
                )
            elif m.group(1) not in seen:
                rejections.append(
                    Rejection(
                        "forward_reference", nid, f"when guard references {m.group(1)!r} "
                        "which is not an earlier step",
                    )
                )

        if step.on_error not in ON_ERROR_CHOICES:
            rejections.append(
                Rejection(
                    "bad_on_error", nid, f"on_error must be one of {ON_ERROR_CHOICES}"
                )
            )
        if step.on_missing is not None and step.on_missing not in ON_MISSING_CHOICES:
            rejections.append(
                Rejection(
                    "bad_on_missing", nid, f"on_missing must be one of {ON_MISSING_CHOICES}"
                )
            )

        # Capability class vs tier (SPEC §10.4: the MCP tier is bounded-only
        # by construction; execute-class ops stay on the visible CLI flow).
        if tier == "mcp" and spec.klass == "execute":
            rejections.append(
                Rejection(
                    "execute_on_observe_tier",
                    nid,
                    f"{step.op} is execute-class; the MCP tier accepts observe-class plans only",
                )
            )

        # Engine availability, judged against the effective on_missing.
        effective_missing = step.on_missing or spec.on_missing_default
        if spec.probe_available is not None and effective_missing == "fail":
            if not spec.probe_available():
                rejections.append(
                    Rejection(
                        "engine_unavailable",
                        nid,
                        f"{step.op} requires an engine that is not installed "
                        f"({spec.engine_hint or 'see op docs'}) and on_missing=fail",
                    )
                )

        # Op-declared static args check (typed, never executes anything).
        if spec.check_args is not None:
            problem = spec.check_args(step.args)
            if problem:
                rejections.append(Rejection("bad_args", nid, problem))

        seen[nid] = out_kind

    return rejections


# ------------------------------------------------------------------ pricing
#: Static cost classes, in abstract units (committed hand-estimates; the
#: telemetry-compiled [plan_engines] epoch table refines physical choice,
#: never these logical priors).
COST_UNITS = {"index": 1, "scan": 8, "process": 40, "test": 120}


def price_plan(plan: EvidencePlan, *, registry: dict[str, Any] | None = None) -> str:
    """Deterministic pre-execution price card (the PRICED-CONTEXT idiom:
    the bill appears before the spend, with the alternative named)."""
    from ctx import plan_ops

    ops = registry if registry is not None else plan_ops.OPS
    lines = [f"[ctx plan price · {len(plan.steps)} nodes · plan:{plan.plan_id()[:12]}]"]
    total = 0
    for step in plan.steps:
        spec = ops.get(step.op)
        cost_class = spec.cost if spec is not None else "scan"
        units = COST_UNITS.get(cost_class, COST_UNITS["scan"])
        mult = step.cap if step.foreach is not None and step.cap else 1
        node_units = units * mult
        total += node_units
        fanout = f" ×{mult} (foreach cap)" if mult > 1 else ""
        guard = f" · when: {step.when}" if step.when else ""
        lines.append(f"  {step.id} · {step.op} · {cost_class}{fanout} ≈ {node_units}u{guard}")
    wall = plan.budget.get("wall_seconds")
    lines.append(
        f"est: {total} units local"
        + (f" · wall budget {wall}s" if wall is not None else "")
        + f" · 1 model round (vs ~{len(plan.steps)} interactive rounds)"
    )
    return "\n".join(lines)


__all__ = [
    "PLAN_SCHEMA",
    "MAX_NODES_HARD",
    "MAX_FANOUT_HARD",
    "REJECTION_VOCABULARY",
    "RANK_KEYS",
    "DEFAULT_RANK_BY",
    "DEFAULT_SECTIONS",
    "PlanError",
    "Rejection",
    "PlanStep",
    "EvidencePlan",
    "parse_plan",
    "validate_plan",
    "price_plan",
]
