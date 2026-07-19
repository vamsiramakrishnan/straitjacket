"""Plan value — deterministic online action ranking from compiled priors.

The online half of the evidence-outcome loop (docs/EVIDENCE-PLANS.md
§plan-value; the offline half is ``evidence_outcomes`` + ``ctx policy
compile --plan-value``):

    predict gain from reviewed historical priors
    execute the highest-value compatible action (or independent batch)
    observe landing / narrowing / discrimination / validation
    write deterministic outcome events
    compile updated priors offline · review · commit

Design laws (all enforced here, tested in tests/test_plan_value.py):

- **Advisory only.** Hard constraints — safety, capability tier, plan
  validity, precision requirements, freshness, evidence-contract floors,
  explicit budgets — are applied BEFORE scoring; a historical prior can
  never resurrect a rejected action.
- **Deterministic + explainable.** Every constant is module-visible;
  every score carries a full explanation record; sorting is total
  (score desc, confidence desc, op name asc). No hidden adaptation:
  runtime never mutates the committed policy.
- **Honest priors.** Low-sample priors shrink toward the global prior and
  then toward the built-in conservative default; the backoff level is
  disclosed. Precision classes discount expected gain — a textual
  fallback is never scored like exact semantic evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# ------------------------------------------------- evidence dimensions

#: Closed evidence-dimension vocabulary (Part 7). Operators declare what
#: they provide; objectives declare required floors.
EVIDENCE_DIMENSIONS = (
    "topology",
    "changedness",
    "dynamic_failure",
    "causality",
    "semantic_support",
    "counterevidence",
    "coverage",
    "freshness",
)

#: Conservative required-floor defaults by objective kind, used when a plan
#: predates the optional ``requires`` field (old plans keep validating).
DEFAULT_FLOORS: dict[str, dict[str, float]] = {
    "diagnose": {
        "dynamic_failure": 1.0,
        "changedness": 1.0,
        "causality": 0.8,
        "counterevidence": 0.5,
    },
    "explore": {"topology": 0.8, "coverage": 0.5},
    "default": {"coverage": 0.5, "causality": 0.5},
}


def required_floors(objective_kind: str, requires: Any = None) -> dict[str, float]:
    """Floors from an explicit ``requires`` list (validated additively by
    plan_ir) or the conservative default for the objective kind."""
    if requires:
        floors: dict[str, float] = {}
        for row in requires:
            dim = str(row.get("dimension") or "")
            if dim in EVIDENCE_DIMENSIONS:
                floors[dim] = max(0.0, min(1.0, float(row.get("floor", 0.0))))
        if floors:
            return floors
    return dict(DEFAULT_FLOORS.get(objective_kind, DEFAULT_FLOORS["default"]))


# ------------------------------------------------------------- constants
#
# Every constant that shapes a score lives HERE, visible and reviewable.
# The committed [plan_value] policy table may override the two thresholds.

WEIGHTS: dict[str, float] = {
    "coverage": 1.0,
    "landing": 0.6,
    "discrimination": 0.5,
    "validation": 0.8,
    "narrowing": 0.4,
    "redundancy": 0.5,   # subtracted
    "reversal": 0.7,     # subtracted
}
CONFIDENCE_FACTOR = {"high": 1.0, "medium": 0.85, "low": 0.6, "insufficient": 0.4}
#: Linear shrinkage toward the fallback prior by confidence class.
SHRINKAGE = {"high": 1.0, "medium": 0.7, "low": 0.35, "insufficient": 0.0}
PRECISION_WEIGHT = {"exact": 1.0, "semantic": 1.0, "structural": 0.85, "textual": 0.6}
FRESHNESS_WEIGHT = {"fresh": 1.0, "stale": 0.6}
#: Deterministic per-cost-class estimates (plan_ir.COST_UNITS lineage).
COST_MS = {"index": 5, "scan": 40, "process": 200, "test": 1200}
COST_VISIBLE_TOK = {"index": 30, "scan": 60, "process": 120, "test": 200}
LOCAL_MS_WEIGHT = 0.05
VISIBLE_TOKEN_WEIGHT = 1.0
PROCESS_SPAWN_PENALTY = 20.0  # applied to process/test cost classes
REFINEMENT_PENALTY = 10.0     # applied to execute-class ops
EPSILON = 0.1
MIN_ACTION_VALUE = 0.25       # stopping threshold (policy-overridable)
MARGINAL_GAIN_THRESHOLD = 0.05  # batch admission floor

#: The built-in conservative prior — the bottom of every backoff chain.
BUILTIN_PRIOR: dict[str, Any] = {
    "landing_rate": 0.30,
    "narrowing_rate": 0.20,
    "discrimination_rate": 0.15,
    "validation_rate": 0.10,
    "retrieval_rate": 0.10,
    "equivalent_requery_rate": 0.10,
    "redundancy_rate": 0.15,
    "reversal_rate": 0.05,
    "observations": 0,
    "confidence": "insufficient",
}

#: Precision class per logical-op prefix (longest prefix wins). Physical
#: engine fallbacks may degrade further at runtime; this is the op's
#: declared ceiling. Language-specific logic never lives here.
PRECISION_OF_OP: tuple[tuple[str, str], ...] = (
    ("semantic.", "semantic"),
    ("ast.", "structural"),
    ("code.refs", "structural"),
    ("code.callers", "structural"),
    ("code.callees", "structural"),
    ("code.impact", "structural"),
    ("code.related_tests", "structural"),
    ("code.search", "textual"),
    ("repo.", "exact"),
    ("evidence.", "exact"),
    ("test.", "exact"),
    ("q.", "exact"),
)


def precision_of(op: str) -> str:
    best = "textual"
    best_len = -1
    for prefix, cls in PRECISION_OF_OP:
        if op.startswith(prefix) and len(prefix) > best_len:
            best, best_len = cls, len(prefix)
    return best


# ----------------------------------------------------------- prior access


def load_priors(ws: Any) -> dict[str, Any]:
    """Read the committed ``[plan_value]`` table from ctx-policy.toml.
    Fail-open to {} — absent priors mean built-in conservative defaults.
    Runtime only ever READS this file."""
    try:
        import tomllib

        path = ws.root / "ctx-policy.toml"
        if not path.is_file():
            return {}
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
        pv = doc.get("plan_value")
        return pv if isinstance(pv, dict) else {}
    except Exception:
        return {}


def lookup_prior(
    priors: Mapping[str, Any],
    op: str,
    *,
    language: str | None = None,
    precision: str | None = None,
    min_observations: int | None = None,
) -> tuple[dict[str, Any], str]:
    """Backoff chain (Part 10), each level skipped when absent or below the
    observation floor: ``op|language|precision`` → ``op|precision`` → ``op``
    → global ``*`` → built-in. Returns (prior_row, disclosed_level)."""
    min_obs = int(
        min_observations
        if min_observations is not None
        else priors.get("minimum_observations", 5)
    )
    chain: list[tuple[str, str]] = []
    if language and precision:
        chain.append((f"{op}|{language}|{precision}", "op+language+precision"))
    if precision:
        chain.append((f"{op}|{precision}", "op+precision"))
    chain.append((op, "op"))
    chain.append(("*", "global"))
    for key, level in chain:
        row = priors.get(key) if isinstance(priors, Mapping) else None
        if isinstance(row, Mapping) and int(row.get("observations", 0)) >= min_obs:
            return dict(row), level
    return dict(BUILTIN_PRIOR), "builtin"


def shrink_prior(row: Mapping[str, Any], fallback: Mapping[str, Any]) -> dict[str, float]:
    """Deterministic linear shrinkage: low-confidence rates move toward the
    fallback prior (Part 6: 'low-confidence priors strongly shrunk')."""
    lam = SHRINKAGE.get(str(row.get("confidence", "insufficient")), 0.0)
    out: dict[str, float] = {}
    for key in BUILTIN_PRIOR:
        if not key.endswith("_rate"):
            continue
        base = float(fallback.get(key, BUILTIN_PRIOR[key]))
        obs = float(row.get(key, base))
        out[key] = round(base + lam * (obs - base), 4)
    return out


# ------------------------------------------------------------ candidates


@dataclass(frozen=True)
class CandidateAction:
    """One applicable logical action. Hard-constraint filtering happens
    BEFORE construction — a candidate in this list is already safe, tier-
    valid, fresh enough, and within budget to consider."""

    op: str
    provides: Mapping[str, float] = field(default_factory=dict)
    cost_class: str = "scan"
    klass: str = "observe"
    language: str | None = None
    fresh: bool = True


@dataclass(frozen=True)
class ScoredAction:
    op: str
    score: float
    coverage_gain: float
    expected: dict[str, float]
    prior_confidence: str
    prior_observations: int
    backoff_level: str
    precision: str
    estimated_ms: int
    estimated_visible_tokens: int
    effective_cost: float
    missing_dimensions: tuple[str, ...]


def candidate_from_spec(op_name: str, spec: Any, **kw: Any) -> CandidateAction:
    """Build a candidate from a registered OpSpec (single source of truth
    for provides/cost/class)."""
    return CandidateAction(
        op=op_name,
        provides=dict(getattr(spec, "provides", None) or {}),
        cost_class=str(getattr(spec, "cost", "scan")),
        klass=str(getattr(spec, "klass", "observe")),
        **kw,
    )


def coverage_gain(
    provides: Mapping[str, float],
    coverage: Mapping[str, float],
    floors: Mapping[str, float],
) -> float:
    """Marginal expected coverage toward the still-missing floors."""
    gain = 0.0
    for dim, floor in floors.items():
        cur = float(coverage.get(dim, 0.0))
        if cur >= floor:
            continue
        gain += max(0.0, min(floor, cur + float(provides.get(dim, 0.0))) - cur)
    return round(gain, 4)


def score_action(
    action: CandidateAction,
    coverage: Mapping[str, float],
    floors: Mapping[str, float],
    priors: Mapping[str, Any],
) -> ScoredAction:
    """The deterministic action-value function (Part 6)."""
    precision = precision_of(action.op)
    global_row, _ = lookup_prior(priors, "*", min_observations=1)
    raw_prior, level = lookup_prior(
        priors, action.op, language=action.language, precision=precision
    )
    fallback = global_row if level != "builtin" else BUILTIN_PRIOR
    rates = shrink_prior(raw_prior, fallback)

    cov = coverage_gain(action.provides, coverage, floors)
    novelty = 1.0 - rates["redundancy_rate"]
    # Relevance gate: the prior-rate terms measure how useful this op's
    # evidence historically was GIVEN the investigation needed it. An action
    # contributing nothing toward the still-missing floors earns no rate
    # credit — otherwise a high-landing op would keep scoring above the
    # stopping threshold forever after the floors are met (priors are a
    # ranking input, never a reason to keep acquiring satisfied evidence).
    relevance = 1.0 if cov > 0 else 0.0
    expected_gain = (
        cov * WEIGHTS["coverage"]
        + relevance
        * (
            rates["landing_rate"] * WEIGHTS["landing"]
            + rates["discrimination_rate"] * WEIGHTS["discrimination"]
            + rates["validation_rate"] * WEIGHTS["validation"]
            + rates["narrowing_rate"] * WEIGHTS["narrowing"]
            - rates["redundancy_rate"] * WEIGHTS["redundancy"]
            - rates["reversal_rate"] * WEIGHTS["reversal"]
        )
    )
    conf = str(raw_prior.get("confidence", "insufficient"))
    effective_gain = (
        expected_gain
        * PRECISION_WEIGHT[precision]
        * FRESHNESS_WEIGHT["fresh" if action.fresh else "stale"]
        * novelty
        * CONFIDENCE_FACTOR.get(conf, CONFIDENCE_FACTOR["insufficient"])
    )
    est_ms = COST_MS.get(action.cost_class, COST_MS["scan"])
    est_tok = COST_VISIBLE_TOK.get(action.cost_class, COST_VISIBLE_TOK["scan"])
    effective_cost = (
        LOCAL_MS_WEIGHT * est_ms
        + VISIBLE_TOKEN_WEIGHT * est_tok
        + (PROCESS_SPAWN_PENALTY if action.cost_class in ("process", "test") else 0.0)
        + (REFINEMENT_PENALTY if action.klass == "execute" else 0.0)
    )
    score = effective_gain / max(effective_cost, EPSILON) * 100.0
    missing = tuple(
        sorted(d for d, f in floors.items() if float(coverage.get(d, 0.0)) < f)
    )
    return ScoredAction(
        op=action.op,
        score=round(score, 2),
        coverage_gain=cov,
        expected={
            "landing": round(rates["landing_rate"], 2),
            "discrimination": round(rates["discrimination_rate"], 2),
            "validation": round(rates["validation_rate"], 2),
            "narrowing": round(rates["narrowing_rate"], 2),
            "redundancy": round(rates["redundancy_rate"], 2),
        },
        prior_confidence=conf,
        prior_observations=int(raw_prior.get("observations", 0)),
        backoff_level=level,
        precision=precision,
        estimated_ms=est_ms,
        estimated_visible_tokens=est_tok,
        effective_cost=round(effective_cost, 2),
        missing_dimensions=missing,
    )


_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "insufficient": 0}


def rank_actions(
    candidates: list[CandidateAction],
    coverage: Mapping[str, float],
    floors: Mapping[str, float],
    priors: Mapping[str, Any],
) -> list[ScoredAction]:
    """Deterministic total order (Part 8): score desc, confidence desc,
    op name asc."""
    scored = [score_action(a, coverage, floors, priors) for a in candidates]
    return sorted(
        scored,
        key=lambda s: (-s.score, -_CONF_RANK.get(s.prior_confidence, 0), s.op),
    )


# ---------------------------------------------------------- batch + stop


def apply_expected_coverage(
    provides: Mapping[str, float], coverage: dict[str, float]
) -> dict[str, float]:
    out = dict(coverage)
    for dim, v in provides.items():
        out[dim] = min(1.0, out.get(dim, 0.0) + float(v))
    return out


def _conflicts(a: CandidateAction, b: CandidateAction) -> bool:
    """Deterministic conflict rule: same op, or two expensive actions that
    provide overlapping dimensions (mutually substitutable — never
    parallelized merely because both score positive)."""
    if a.op == b.op:
        return True
    if a.cost_class in ("process", "test") and b.cost_class in ("process", "test"):
        return bool(set(a.provides) & set(b.provides))
    return False


def select_batch(
    candidates: list[CandidateAction],
    coverage: Mapping[str, float],
    floors: Mapping[str, float],
    priors: Mapping[str, Any],
    *,
    budget_units: int = 200,
    marginal_threshold: float = MARGINAL_GAIN_THRESHOLD,
) -> list[ScoredAction]:
    """Greedy deterministic batch (Part 8 pseudo-code, verbatim semantics):
    admit by rank while marginal coverage stays above threshold, no
    conflicts, and the plan_ir cost-unit budget holds."""
    from ctx.plan_ir import COST_UNITS

    by_op = {c.op: c for c in candidates}
    ranked = rank_actions(candidates, coverage, floors, priors)
    selected: list[ScoredAction] = []
    selected_actions: list[CandidateAction] = []
    covered = dict(coverage)
    spent = 0
    for s in ranked:
        cand = by_op[s.op]
        marginal = coverage_gain(cand.provides, covered, floors)
        if selected and marginal < marginal_threshold:
            continue
        if any(_conflicts(cand, prev) for prev in selected_actions):
            continue
        units = COST_UNITS.get(cand.cost_class, COST_UNITS["scan"])
        if spent + units > budget_units:
            continue
        selected.append(s)
        selected_actions.append(cand)
        covered = apply_expected_coverage(cand.provides, covered)
        spent += units
    return selected


def stopping_decision(
    ranked: list[ScoredAction],
    coverage: Mapping[str, float],
    floors: Mapping[str, float],
    *,
    threshold: float | None = None,
    priors: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Advisory stopping rule (Part 9): stop when every floor is met AND no
    remaining action beats the value threshold. Never suppresses mandatory
    verifier steps or explicit user requests — the caller owns that."""
    thr = float(
        threshold
        if threshold is not None
        else (priors or {}).get("min_action_value", MIN_ACTION_VALUE)
    )
    lines = ["evidence acquisition receipt", "required floors:"]
    floors_met = True
    for dim in sorted(floors):
        cur = float(coverage.get(dim, 0.0))
        met = cur >= floors[dim]
        floors_met &= met
        lines.append(f"  {dim:<18} {cur:.2f} / {floors[dim]:.2f}" + ("" if met else "  UNMET"))
    best = ranked[0] if ranked else None
    if best is not None:
        lines += [
            "best remaining action:",
            f"  {best.op}",
            f"  value score: {best.score:.2f}",
        ]
    lines += ["policy threshold:", f"  {thr:.2f}"]
    stop = floors_met and (best is None or best.score < thr)
    lines.insert(0, "evidence acquisition stopped" if stop else "evidence acquisition continues")
    return stop, "\n".join(lines)


# ---------------------------------------------------------- explanation


def render_ranking(
    ranked: list[ScoredAction], *, selected: "ScoredAction | None" = None
) -> str:
    """The inspectable score explanation (Part 6). Two decimals — never
    more precision than the priors support."""
    if not ranked:
        return "no applicable candidate actions"
    sel = selected or ranked[0]
    out = [
        f"candidate action: {sel.op}",
        "applicable: yes",
        "missing dimensions: " + (", ".join(sel.missing_dimensions) or "(none)"),
        f"prior confidence: {sel.prior_confidence} "
        f"({sel.prior_observations} observations · backoff: {sel.backoff_level})",
        "expected:",
        f"  landing        {sel.expected['landing']:.2f}",
        f"  discrimination {sel.expected['discrimination']:.2f}",
        f"  validation     {sel.expected['validation']:.2f}",
        f"  redundancy     {sel.expected['redundancy']:.2f}",
        "estimated cost:",
        f"  local          {sel.estimated_ms} ms",
        f"  visible        {sel.estimated_visible_tokens} tokens",
        "value score:",
        f"  {sel.score:.2f}",
    ]
    others = [s for s in ranked if s.op != sel.op]
    if others:
        out.append("selected over:")
        for s in others[:6]:
            out.append(f"  {s.op:<24} {s.score:.2f}")
    return "\n".join(out)


__all__ = [
    "EVIDENCE_DIMENSIONS",
    "DEFAULT_FLOORS",
    "required_floors",
    "WEIGHTS",
    "BUILTIN_PRIOR",
    "CandidateAction",
    "ScoredAction",
    "candidate_from_spec",
    "precision_of",
    "load_priors",
    "lookup_prior",
    "shrink_prior",
    "coverage_gain",
    "score_action",
    "rank_actions",
    "select_batch",
    "apply_expected_coverage",
    "stopping_decision",
    "render_ranking",
]
