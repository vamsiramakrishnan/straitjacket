"""Plan value — per-operator follow-up statistics and shadow ranking.

The online half of the evidence-followup loop (the offline half is
``evidence_outcomes.followup_join`` + ``ctx policy compile --plan-value``).
Scope, deliberately narrow:

1. **Report** — per-operator follow-up statistics (counts first, rates
   derived, Wilson lower bounds for ranking).
2. **Shadow ranking** — given already-applicable candidates, what the
   empirical ordering would have preferred, with the lexicographic key
   disclosed. Report-only: nothing is reordered, inserted, or suppressed.
3. **Promotion path** — only after a paired referee shows the shadow
   ordering beating declared orderings at equal task success may the
   ranking be used online, and then only as a conservative tie-break
   between actions already equivalent under hard semantics.

What this module deliberately does NOT do (design-review verdict,
2026-07-19): no weighted utility scalar, no fractional evidence-coverage
arithmetic, no confidence floats, no automatic stopping, no batch
scheduling, no language-partitioned cells. The evidence-dimension
vocabulary survives only as *descriptive* plan metadata (``requires``
floors are displayed, never enforced) — converting a qualitative model
into a quantitative type system waits until the quantities predict
something.

Ranking is lexicographic, not weighted soup:

    1. hard constraints (caller-side: safety, tier, validity, contract)
    2. precision class (exact/semantic before structural before textual)
    3. freshness class (fresh before stale)
    4. Wilson lower bound of exact-use rate, descending
    5. Wilson lower bound of validation-association rate, descending
    6. equivalent-requery rate, ascending
    7. median visible tokens, ascending
    8. median cost ms, ascending
    9. operator name, ascending

The Wilson lower bound is the entire sample-size treatment: 2/2 cannot
outrank 68/84, and no confidence-class or shrinkage table exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

# ------------------------------------------------- evidence dimensions
#
# DESCRIPTIVE vocabulary only: plan_ir validates `requires` against it and
# the shadow report displays UNMET floors as information. Nothing scores,
# gates, or stops on these values.
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
    plan_ir) or the conservative default for the objective kind. Display
    semantics only."""
    if requires:
        floors: dict[str, float] = {}
        for row in requires:
            dim = str(row.get("dimension") or "")
            if dim in EVIDENCE_DIMENSIONS:
                floors[dim] = max(0.0, min(1.0, float(row.get("floor", 0.0))))
        if floors:
            return floors
    return dict(DEFAULT_FLOORS.get(objective_kind, DEFAULT_FLOORS["default"]))


def realized_coverage(steps: Any, node_rows: Mapping[str, int] | None) -> dict[str, float]:
    """Coverage credited only for ops whose node produced >= 1 row — a
    declared ``provides`` beside an empty join is a claim, not evidence.
    Used exclusively for the UNMET-floors display line."""
    from ctx import plan_ops

    covered: dict[str, float] = {}
    rows = dict(node_rows or {})
    credited: set[str] = set()
    for step in steps:
        if rows.get(step.id, 0) < 1 or step.op in credited:
            continue
        spec = plan_ops.OPS.get(step.op)
        if spec is None or not spec.provides:
            continue
        credited.add(step.op)
        for dim, v in spec.provides.items():
            covered[dim] = min(1.0, covered.get(dim, 0.0) + float(v))
    return covered


# --------------------------------------------------------- precision class

#: Precision class per logical-op prefix (longest prefix wins). The op's
#: declared ceiling; physical fallbacks may degrade further at runtime.
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
_PRECISION_RANK = {"exact": 0, "semantic": 0, "structural": 1, "textual": 2}


def precision_of(op: str) -> str:
    best = "textual"
    best_len = -1
    for prefix, cls in PRECISION_OF_OP:
        if op.startswith(prefix) and len(prefix) > best_len:
            best, best_len = cls, len(prefix)
    return best


# ----------------------------------------------------------- follow-up stats


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound — the standard small-sample
    correction. 0.0 when n == 0 (no observations claim nothing)."""
    if n <= 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def load_priors(ws: Any) -> dict[str, Any]:
    """Read the committed ``[plan_value]`` follow-up table from
    ctx-policy.toml. Fail-open to {}. Runtime only ever READS this file."""
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


@dataclass(frozen=True)
class CandidateAction:
    """One already-applicable action. Hard-constraint filtering happens
    BEFORE construction — a candidate here is safe, tier-valid, and within
    budget to consider. The ranking never resurrects a rejected action."""

    op: str
    cost_class: str = "scan"
    klass: str = "observe"
    fresh: bool = True


@dataclass(frozen=True)
class FollowupRank:
    """One ranked candidate with the full lexicographic key disclosed —
    the explanation IS the key, no scalar score exists to hide behind."""

    op: str
    precision: str
    fresh: bool
    n: int
    used_exactly: int
    validation_associated: int
    equivalent_requery: int
    censored: int
    wilson_used: float
    wilson_validation: float
    requery_rate: float
    median_visible_tokens: int
    median_cost_ms: int

    def sort_key(self) -> tuple:
        return (
            _PRECISION_RANK.get(self.precision, 3),
            0 if self.fresh else 1,
            -round(self.wilson_used, 4),
            -round(self.wilson_validation, 4),
            round(self.requery_rate, 4),
            self.median_visible_tokens,
            self.median_cost_ms,
            self.op,
        )


#: Deterministic fallback cost estimates by cost class, used when the
#: compiled table carries no observed medians for an operator.
FALLBACK_MS = {"index": 5, "scan": 40, "process": 200, "test": 1200}
FALLBACK_TOK = {"index": 30, "scan": 60, "process": 120, "test": 200}


def _stat_row(priors: Mapping[str, Any], op: str) -> Mapping[str, Any]:
    row = priors.get(op)
    return row if isinstance(row, Mapping) else {}


def rank_followup(
    candidates: list[CandidateAction],
    priors: Mapping[str, Any],
) -> list[FollowupRank]:
    """Shadow ranking of already-applicable candidates by the lexicographic
    rule. Deterministic and total; ties end at the operator name."""
    ranked: list[FollowupRank] = []
    for c in candidates:
        row = _stat_row(priors, c.op)
        n = int(row.get("observations", 0))
        used = int(row.get("used_exactly", 0))
        val = int(row.get("validation_associated", 0))
        req = int(row.get("equivalent_requery", 0))
        cen = int(row.get("censored", 0))
        non_censored = max(0, n - cen)
        ranked.append(
            FollowupRank(
                op=c.op,
                precision=precision_of(c.op),
                fresh=c.fresh,
                n=n,
                used_exactly=used,
                validation_associated=val,
                equivalent_requery=req,
                censored=cen,
                # Positive numerators over ALL observations (censoring can
                # only under-count positives — conservative); the negative
                # requery rate excludes censored from its denominator.
                wilson_used=round(wilson_lower_bound(used, n), 4),
                wilson_validation=round(wilson_lower_bound(val, n), 4),
                requery_rate=round(req / non_censored, 4) if non_censored else 0.0,
                median_visible_tokens=int(
                    row.get("median_visible_tokens", FALLBACK_TOK.get(c.cost_class, 60))
                ),
                median_cost_ms=int(
                    row.get("median_cost_ms", FALLBACK_MS.get(c.cost_class, 40))
                ),
            )
        )
    return sorted(ranked, key=lambda r: r.sort_key())


def render_shadow(
    declared_first: str | None,
    ranked: list[FollowupRank],
    *,
    floors: Mapping[str, float] | None = None,
    coverage: Mapping[str, float] | None = None,
) -> str:
    """The shadow report (report only — never reorders): declared vs
    shadow-preferred with the lexicographic reason, per-candidate counts,
    and the descriptive floors display. Low-yield is an advisory sentence,
    never a suppression."""
    lines = ["── operator follow-up shadow (report only; never reorders) ──"]
    if floors:
        lines.append("declared evidence floors (descriptive):")
        cov = coverage or {}
        for dim in sorted(floors):
            cur = float(cov.get(dim, 0.0))
            met = "" if cur >= floors[dim] else "  UNMET"
            lines.append(f"  {dim:<18} {cur:.2f} / {floors[dim]:.2f}{met}")
    if not ranked:
        lines.append("no applicable candidates to rank")
        return "\n".join(lines)
    shadow = ranked[0]
    lines.append(f"declared first: {declared_first or '(none declared)'}")
    lines.append(f"shadow preferred: {shadow.op}")
    agree = declared_first == shadow.op
    lines.append(f"agreement: {'yes' if agree else 'no'}")
    lines.append(
        "reason (lexicographic): "
        f"precision={shadow.precision} · "
        f"wilson(used {shadow.used_exactly}/{shadow.n})={shadow.wilson_used:.2f} · "
        f"wilson(valid {shadow.validation_associated}/{shadow.n})={shadow.wilson_validation:.2f} · "
        f"requery={shadow.requery_rate:.2f} · "
        f"~{shadow.median_visible_tokens} tok · ~{shadow.median_cost_ms} ms"
    )
    lines.append(f"{'operator':<28} {'n':>4} {'used':>5} {'valid':>5} {'requery':>7} {'tok':>5} {'ms':>6}")
    for r in ranked[:8]:
        lines.append(
            f"{r.op:<28} {r.n:>4} {r.used_exactly:>5} {r.validation_associated:>5}"
            f" {r.equivalent_requery:>7} {r.median_visible_tokens:>5} {r.median_cost_ms:>6}"
        )
    if all(r.wilson_used == 0.0 for r in ranked):
        lines.append(
            "advisory: no operator has demonstrated follow-up yet — "
            "additional evidence appears low-yield (nothing is suppressed)"
        )
    return "\n".join(lines)


__all__ = [
    "EVIDENCE_DIMENSIONS",
    "DEFAULT_FLOORS",
    "required_floors",
    "realized_coverage",
    "PRECISION_OF_OP",
    "precision_of",
    "wilson_lower_bound",
    "load_priors",
    "CandidateAction",
    "FollowupRank",
    "rank_followup",
    "render_shadow",
]
