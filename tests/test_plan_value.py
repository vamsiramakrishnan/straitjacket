"""Acceptance: plan_value — Wilson lexicographic shadow ranking (the
ponytail cut: no weighted utility, no confidence floats, no stopping
verdict, no batch scheduler)."""

from ctx import plan_value as pv
from ctx.plan_value import (
    CandidateAction,
    rank_followup,
    realized_coverage,
    render_shadow,
    required_floors,
    wilson_lower_bound,
)

# A committed-style counts table (what compile_plan_value emits).
PRIORS = {
    "evidence.join": {
        "observations": 84, "used_exactly": 68, "validation_associated": 40,
        "equivalent_requery": 3, "censored": 5,
        "median_cost_ms": 12, "median_visible_tokens": 48,
    },
    "code.refs": {
        "observations": 51, "used_exactly": 27, "validation_associated": 14,
        "equivalent_requery": 7, "censored": 4,
        "median_cost_ms": 95, "median_visible_tokens": 73,
    },
    "semantic.taint": {
        "observations": 22, "used_exactly": 4, "validation_associated": 2,
        "equivalent_requery": 6, "censored": 3,
        "median_cost_ms": 1800, "median_visible_tokens": 121,
    },
    "code.tiny_sample": {
        "observations": 2, "used_exactly": 2, "validation_associated": 2,
        "equivalent_requery": 0, "censored": 0,
        "median_cost_ms": 1, "median_visible_tokens": 5,
    },
}

JOIN = CandidateAction(op="evidence.join", cost_class="index")
REFS = CandidateAction(op="code.refs", cost_class="scan")
TAINT = CandidateAction(op="semantic.taint", cost_class="process")
TINY = CandidateAction(op="code.tiny_sample", cost_class="scan")


def test_wilson_lower_bound_basics():
    assert wilson_lower_bound(0, 0) == 0.0
    assert 0.0 < wilson_lower_bound(2, 2) < 1.0  # 2/2 is not certainty
    assert wilson_lower_bound(68, 84) > wilson_lower_bound(2, 2)
    assert wilson_lower_bound(68, 84) < 68 / 84  # a lower bound, not the MLE


def test_small_samples_cannot_outrank_large_evidence():
    """2/2 = '100%' must not beat 68/84 — the entire sample-size treatment
    is one standard formula, not a confidence-class subsystem."""
    ranked = rank_followup([JOIN, TINY], PRIORS)
    assert ranked[0].op == "evidence.join"


def test_lexicographic_order_and_disclosure():
    ranked = rank_followup([JOIN, REFS, TAINT], PRIORS)
    assert [r.op for r in ranked][0] == "evidence.join"
    # Precision is senior to statistics: semantic taint (precision rank 0)
    # precedes structural refs despite far weaker follow-up counts.
    assert [r.op for r in ranked] == ["evidence.join", "semantic.taint", "code.refs"]
    top = ranked[0]
    # The explanation IS the key: counts and medians are all disclosed.
    assert top.n == 84 and top.used_exactly == 68
    assert top.median_cost_ms == 12
    # Deterministic: same inputs, same order.
    assert [r.op for r in rank_followup([TAINT, REFS, JOIN], PRIORS)] == [
        r.op for r in ranked
    ]


def test_precision_class_precedes_statistics():
    """A textual op can never outrank an exact op on statistics alone —
    exact precision over approximate is lexicographically senior."""
    textual_hot = CandidateAction(op="code.search", cost_class="scan")
    priors = {
        "code.search": {"observations": 100, "used_exactly": 99,
                        "validation_associated": 90, "equivalent_requery": 0,
                        "censored": 0},
        "evidence.join": {"observations": 10, "used_exactly": 3,
                          "validation_associated": 1, "equivalent_requery": 1,
                          "censored": 1},
    }
    ranked = rank_followup([textual_hot, JOIN], priors)
    assert ranked[0].op == "evidence.join"  # exact > textual, regardless of rates


def test_unknown_operator_ranks_by_name_at_zero():
    ranked = rank_followup(
        [CandidateAction(op="evidence.bbb"), CandidateAction(op="evidence.aaa")], {}
    )
    assert [r.op for r in ranked] == ["evidence.aaa", "evidence.bbb"]
    assert all(r.wilson_used == 0.0 for r in ranked)


def test_render_shadow_report_only():
    ranked = rank_followup([JOIN, TAINT], PRIORS)
    out = render_shadow("semantic.taint", ranked,
                        floors={"causality": 0.8}, coverage={"causality": 0.0})
    assert "report only; never reorders" in out
    assert "declared first: semantic.taint" in out
    assert "shadow preferred: evidence.join" in out
    assert "agreement: no" in out
    assert "68/84" in out.replace(" ", "") or "68" in out  # counts visible
    assert "UNMET" in out  # descriptive floors display
    # No scalar score anywhere — the lexicographic reason is the explanation.
    assert "value score" not in out


def test_low_yield_advisory_never_suppresses():
    ranked = rank_followup([CandidateAction(op="evidence.join")], {})
    out = render_shadow("evidence.join", ranked)
    assert "low-yield" in out and "nothing is suppressed" in out


def test_hard_constraints_dominate():
    """A high-value execute-class action stays rejected on an observe-only
    tier: validate_plan is the enforcement point, before any ranking."""
    from ctx import plan_ir

    doc = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": "q"},
        "steps": [{"id": "t", "op": "test.run", "args": {"command": "pytest -q"}}],
    }
    plan = plan_ir.parse_plan(doc)
    rejections = plan_ir.validate_plan(plan, tier="mcp")
    assert any(r.reason == "execute_on_observe_tier" for r in rejections)


def test_requires_floors_descriptive_defaults():
    floors = required_floors("diagnose", None)
    assert floors["dynamic_failure"] == 1.0
    explicit = required_floors(
        "diagnose", [{"dimension": "counterevidence", "floor": 0.5}]
    )
    assert explicit == {"counterevidence": 0.5}
    assert required_floors("diagnose", [{"dimension": "vibes", "floor": 1.0}]) == floors


def test_old_plans_still_validate_and_new_requires_is_checked():
    from ctx import plan_ir

    old = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": "q"},
        "steps": [{"id": "a", "op": "repo.changed"}],
    }
    plan = plan_ir.parse_plan(old)
    assert plan.requires == ()
    assert plan_ir.validate_plan(plan) == []
    bad = dict(old, objective={"kind": "diagnose", "question": "q",
                               "requires": [{"dimension": "vibes", "floor": 2.0}]})
    rejections = plan_ir.validate_plan(plan_ir.parse_plan(bad))
    assert any(r.reason == "bad_requires" for r in rejections)


def test_realized_coverage_requires_rows():
    from ctx import plan_ir

    doc = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": "q"},
        "steps": [
            {"id": "c", "op": "repo.changed"},
            {"id": "j", "op": "evidence.join", "args": {"on": "failing_in_changed"},
             "after": ["c"]},
        ],
    }
    plan = plan_ir.parse_plan(doc)
    # The empty join earns nothing; the row-producing change op earns credit.
    cov = realized_coverage(plan.steps, {"c": 3, "j": 0})
    assert cov.get("changedness", 0) > 0
    assert cov.get("causality", 0) == 0  # declared 1.0 beside an empty join

    assert pv.precision_of("semantic.taint") == "semantic"
    assert pv.precision_of("code.search") == "textual"
