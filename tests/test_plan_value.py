"""Acceptance: plan_value — deterministic online action ranking from
compiled priors (docs/EVIDENCE-PLANS.md §plan-value)."""

from ctx import plan_value as pv
from ctx.plan_value import (
    BUILTIN_PRIOR,
    CandidateAction,
    coverage_gain,
    lookup_prior,
    rank_actions,
    required_floors,
    score_action,
    select_batch,
    shrink_prior,
    stopping_decision,
)

# A committed-style priors table (what load_priors returns).
PRIORS = {
    "version": 1,
    "minimum_observations": 5,
    "min_action_value": 0.25,
    "*": {
        "observations": 160, "confidence": "high",
        "landing_rate": 0.40, "narrowing_rate": 0.30, "discrimination_rate": 0.20,
        "validation_rate": 0.20, "retrieval_rate": 0.15,
        "equivalent_requery_rate": 0.08, "redundancy_rate": 0.10, "reversal_rate": 0.03,
    },
    "evidence.join": {
        "observations": 84, "confidence": "high",
        "landing_rate": 0.79, "narrowing_rate": 0.71, "discrimination_rate": 0.63,
        "validation_rate": 0.54, "retrieval_rate": 0.22,
        "equivalent_requery_rate": 0.03, "redundancy_rate": 0.05, "reversal_rate": 0.02,
    },
    "semantic.taint": {
        "observations": 22, "confidence": "medium",
        "landing_rate": 0.18, "narrowing_rate": 0.14, "discrimination_rate": 0.10,
        "validation_rate": 0.09, "retrieval_rate": 0.05,
        "equivalent_requery_rate": 0.10, "redundancy_rate": 0.32, "reversal_rate": 0.05,
    },
    "code.refs": {
        "observations": 3, "confidence": "insufficient",
        "landing_rate": 0.99, "narrowing_rate": 0.99, "discrimination_rate": 0.99,
        "validation_rate": 0.99, "retrieval_rate": 0.99,
        "equivalent_requery_rate": 0.0, "redundancy_rate": 0.0, "reversal_rate": 0.0,
    },
}

JOIN = CandidateAction(
    op="evidence.join",
    provides={"causality": 1.0, "changedness": 0.8, "dynamic_failure": 0.4},
    cost_class="index",
)
TAINT = CandidateAction(
    op="semantic.taint",
    provides={"semantic_support": 1.0, "counterevidence": 0.5},
    cost_class="process",
)
REFS = CandidateAction(
    op="code.refs", provides={"topology": 0.8, "semantic_support": 0.3},
    cost_class="scan",
)
TEST_RUN = CandidateAction(
    op="test.run",
    provides={"dynamic_failure": 1.0, "freshness": 0.8, "coverage": 0.3},
    cost_class="test", klass="execute",
)

FLOORS = {"causality": 0.8, "changedness": 1.0, "dynamic_failure": 1.0,
          "counterevidence": 0.5}


def test_backoff_chain_is_disclosed():
    row, level = lookup_prior(PRIORS, "evidence.join")
    assert level == "op" and row["observations"] == 84
    row, level = lookup_prior(PRIORS, "evidence.join", language="py", precision="exact")
    assert level == "op"  # partitioned keys absent → falls through, disclosed
    row, level = lookup_prior(PRIORS, "ast.search")
    assert level == "global" and row["observations"] == 160
    row, level = lookup_prior({}, "ast.search")
    assert level == "builtin" and row == BUILTIN_PRIOR


def test_sparse_priors_shrink_toward_fallback():
    # code.refs has 3 observations of fantasy rates: below minimum, the
    # backoff skips it entirely (global row wins) — the fantasy never leaks.
    row, level = lookup_prior(PRIORS, "code.refs")
    assert level == "global"
    # And an insufficient-confidence row passed to shrink collapses fully
    # onto the fallback (SHRINKAGE weight 0.0).
    shrunk = shrink_prior(PRIORS["code.refs"], PRIORS["*"])
    assert shrunk["landing_rate"] == PRIORS["*"]["landing_rate"]


def test_score_explanations_match_inputs():
    s = score_action(JOIN, {}, FLOORS, PRIORS)
    assert s.prior_confidence == "high" and s.prior_observations == 84
    assert s.expected["landing"] == 0.79
    assert s.backoff_level == "op"
    assert s.precision == "exact"
    assert set(s.missing_dimensions) == set(FLOORS)
    assert s.coverage_gain > 0
    rendered = pv.render_ranking([s])
    assert "evidence.join" in rendered and "0.79" in rendered
    assert f"{s.score:.2f}" in rendered


def test_fixture_a_cheap_join_wins():
    """Fixture A: changed files + failures known, causality missing — the
    cheap high-prior join must outrank refs, taint, and a raw test rerun."""
    coverage = {"changedness": 1.0, "dynamic_failure": 1.0}
    floors = FLOORS
    ranked = rank_actions([JOIN, REFS, TAINT, TEST_RUN], coverage, floors, PRIORS)
    assert ranked[0].op == "evidence.join"
    assert ranked[0].score > ranked[1].score


def test_fixture_b_expensive_semantic_deferred_then_applicable():
    """Fixture B: nothing known — dynamic/changedness actions outrank the
    Semgrep scan; once cheaper actions leave only the semantic question,
    taint rises to the top of the remaining candidates."""
    floors = {"dynamic_failure": 1.0, "changedness": 1.0, "semantic_support": 0.5}
    ranked = rank_actions([TAINT, TEST_RUN, JOIN, REFS], {}, floors, PRIORS)
    assert ranked[0].op != "semantic.taint"
    # Cheap partial coverage first: refs' 0.3 semantic_support at scan cost
    # legitimately precedes the process-class scan (value-per-cost).
    covered = {"dynamic_failure": 1.0, "changedness": 1.0, "causality": 1.0}
    ranked_mid = rank_actions([TAINT, REFS], covered, floors, PRIORS)
    assert ranked_mid[0].op == "code.refs"
    # After the cheaper actions have RUN (refs included) and the
    # source-to-sink question remains, taint finally ranks first.
    covered2 = dict(covered, semantic_support=0.3)
    ranked2 = rank_actions([TAINT, TEST_RUN], covered2, floors, PRIORS)
    assert ranked2[0].op == "semantic.taint"


def test_hard_constraints_dominate():
    """A high-value execute-class action stays rejected on an observe-only
    tier: constraint filtering happens BEFORE scoring, and validate_plan is
    the enforcement point."""
    from ctx import plan_ir

    doc = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": "q"},
        "steps": [{"id": "t", "op": "test.run", "args": {"command": "pytest -q"}}],
    }
    plan = plan_ir.parse_plan(doc)
    rejections = plan_ir.validate_plan(plan, tier="mcp")
    assert any(r.reason == "execute_on_observe_tier" for r in rejections)


def test_batch_is_greedy_deterministic_and_non_substitutable():
    floors = {"causality": 0.8, "semantic_support": 0.5, "dynamic_failure": 1.0}
    batch = select_batch([JOIN, TAINT, TEST_RUN, REFS], {}, floors, PRIORS)
    ops = [s.op for s in batch]
    assert ops == [s.op for s in select_batch([JOIN, TAINT, TEST_RUN, REFS], {}, floors, PRIORS)]
    # join + taint cover different high-priority dims → both admissible;
    # a second expensive action sharing provided dims would be excluded.
    assert "evidence.join" in ops
    two_expensive = [TAINT, CandidateAction(op="semantic.policy_scan",
                                            provides={"semantic_support": 1.0},
                                            cost_class="process")]
    batch2 = select_batch(two_expensive, {}, {"semantic_support": 1.0}, PRIORS)
    assert len(batch2) == 1  # mutually substitutable expensive scans never pair


def test_stopping_rule():
    floors = {"dynamic_failure": 1.0, "causality": 0.8}
    full = {"dynamic_failure": 1.0, "causality": 0.91}
    weak = [score_action(TAINT, full, floors, PRIORS)]
    stop, receipt = stopping_decision(weak, full, floors, priors=PRIORS)
    assert stop
    assert "evidence acquisition stopped" in receipt
    assert "policy threshold" in receipt
    # An unmet floor forbids stopping regardless of scores.
    partial = {"dynamic_failure": 1.0, "causality": 0.2}
    stop2, receipt2 = stopping_decision([], partial, floors, priors=PRIORS)
    assert not stop2 and "UNMET" in receipt2


def test_requires_floors_and_conservative_defaults():
    floors = required_floors("diagnose", None)
    assert floors["dynamic_failure"] == 1.0  # conservative default by kind
    explicit = required_floors(
        "diagnose", [{"dimension": "counterevidence", "floor": 0.5}]
    )
    assert explicit == {"counterevidence": 0.5}
    # Unknown dimensions are dropped (validate_plan rejects them upstream).
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


def test_coverage_gain_clips_at_floor():
    assert coverage_gain({"causality": 1.0}, {"causality": 0.7}, {"causality": 0.8}) == 0.1
    assert coverage_gain({"causality": 1.0}, {"causality": 0.9}, {"causality": 0.8}) == 0.0
