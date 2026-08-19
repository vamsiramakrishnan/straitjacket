from __future__ import annotations

import pytest

from evals.alphaevolve.choice_eval import case as choice_case
from evals.alphaevolve.choice_eval import option as choice_option
from evals.alphaevolve.choice_eval import score_choice_policy
from evals.alphaevolve.context_budget import evaluate as context_budget
from evals.alphaevolve.naive_fast_path import evaluate as naive_fast_path
from evals.alphaevolve.route_policy import evaluate as route_policy
from evals.alphaevolve.route_replay import evaluate as route_replay
from evals.alphaevolve.turn_policy import evaluate as turn_policy
from evals.alphaevolve.escalation_policy import evaluate as escalation_policy
from evals.alphaevolve.execution_policy import evaluate as execution_policy
from evals.alphaevolve.guard_policy import evaluate as guard_policy
from evals.alphaevolve.portfolio import (
    EXPERIMENTS,
    load_experiment,
    local_scorecard,
    promotion_report,
    readiness_report,
    shadow_report,
)
from evals.alphaevolve.registry import LEVERS, experiment_fingerprint, registry_document


ALL_EXPERIMENTS = tuple(load_experiment(name) for name in EXPERIMENTS)


@pytest.mark.parametrize(
    "experiment",
    ALL_EXPERIMENTS,
)
def test_portfolio_seed_passes_every_completion_gate(experiment):
    result = experiment.score_candidate(experiment.INITIAL_PROGRAM_CODE)
    assert result["error"] is None, result
    assert result["score"] > 0
    assert all(row["completion"] == 1.0 for row in result["cases"].values())


@pytest.mark.parametrize(
    "experiment",
    ALL_EXPERIMENTS,
)
def test_portfolio_controller_adapter_has_metric_shape(experiment):
    candidate = {
        "content": {"files": [{"content": experiment.INITIAL_PROGRAM_CODE}]}
    }
    result = experiment.evaluation_function(candidate)
    score = result["scores"]["scores"][0]
    assert score["metric"] == experiment.METRIC_NAME
    assert score["score"] > 0


def test_context_budget_rejects_silent_omission():
    candidate = context_budget.INITIAL_PROGRAM_CODE.replace(
        "    return sorted(selected)",
        "    return []",
    )
    result = context_budget.score_candidate(candidate)
    assert result["score"] < 0
    assert "mandatory evidence" in result["error"]


def test_context_budget_seed_passes_holdout_and_adversarial():
    assert context_budget.score_holdout(context_budget.INITIAL_PROGRAM_CODE)["error"] is None
    assert context_budget.score_adversarial(context_budget.INITIAL_PROGRAM_CODE)["error"] is None


def test_turn_policy_rejects_an_unsafe_action():
    candidate = turn_policy.INITIAL_PROGRAM_CODE.replace(
        "    available = {str(action[\"id\"]): action for action in state.get(\"available_actions\", ())}",
        "    available = {str(action[\"id\"]): action for action in state.get(\"available_actions\", ())}\n    if \"shell_loop\" in available:\n        return \"shell_loop\"",
    )
    result = turn_policy.score_candidate(candidate)
    assert result["score"] < 0
    assert "unsafe action" in result["error"]


def test_turn_policy_seed_passes_holdout_and_adversarial():
    assert turn_policy.score_holdout(turn_policy.INITIAL_PROGRAM_CODE)["error"] is None
    assert turn_policy.score_adversarial(turn_policy.INITIAL_PROGRAM_CODE)["error"] is None


def test_route_policy_rejects_cheap_non_completion():
    candidate = route_policy.INITIAL_PROGRAM_CODE.replace(
        "    complexity = int(task.get(\"complexity\", 3))",
        "    return \"economy_single\"\n    complexity = int(task.get(\"complexity\", 3))",
    )
    result = route_policy.score_candidate(candidate)
    assert result["score"] < 0
    assert "non-completing routes" in result["error"]


def test_route_policy_seed_passes_holdout_and_adversarial():
    assert route_policy.score_holdout(route_policy.INITIAL_PROGRAM_CODE)["error"] is None
    assert route_policy.score_adversarial(route_policy.INITIAL_PROGRAM_CODE)["error"] is None


def test_escalation_policy_rejects_spending_more_on_permission_failure():
    candidate = escalation_policy.INITIAL_PROGRAM_CODE.replace(
        '        preferences = ["stop_blocked"]',
        '        preferences = ["escalate"]',
    )
    result = escalation_policy.score_candidate(candidate)
    assert result["score"] < 0
    assert "permission_prompt" in result["error"]


def test_naive_seed_is_the_complete_but_inefficient_baseline():
    result = naive_fast_path.score_candidate(naive_fast_path.INITIAL_PROGRAM_CODE)
    assert result["error"] is None
    assert result["score"] == pytest.approx(100.0)
    assert result["pareto_beats_naive_direct"] is False


def test_focused_naive_policy_beats_both_naive_baselines_and_holdout():
    candidate = naive_fast_path.INTEGRATED_PROGRAM_CODE
    search = naive_fast_path.score_candidate(candidate)
    holdout = naive_fast_path.score_holdout(candidate)
    adversarial = naive_fast_path.score_adversarial(candidate)
    assert search["error"] is None
    assert holdout["error"] is None
    assert adversarial["error"] is None
    assert search["score"] > 100.0
    assert search["pareto_beats_naive_direct"] is True
    assert holdout["pareto_beats_naive_direct"] is True
    assert adversarial["pareto_beats_naive_direct"] is True


def test_naive_fast_path_rejects_the_cheap_noop():
    candidate = naive_fast_path.INITIAL_PROGRAM_CODE.replace(
        "    for plan in plans:",
        '    return "cheap_noop"\n    for plan in plans:',
    )
    result = naive_fast_path.score_candidate(candidate)
    assert result["score"] < 0
    assert "incomplete naive tasks" in result["error"]


def test_route_replay_rejects_interactive_empirical_failure():
    candidate = route_replay.INITIAL_PROGRAM_CODE.replace(
        '        "test": "proven_unattended_test",',
        '        "test": "failed_interactive_test",',
    )
    result = route_replay.score_candidate(candidate)
    assert result["score"] < 0
    assert "live_named_test" in result["error"]


def test_route_replay_seed_passes_holdout_and_adversarial():
    assert route_replay.score_holdout(route_replay.INITIAL_PROGRAM_CODE)["error"] is None
    assert route_replay.score_adversarial(route_replay.INITIAL_PROGRAM_CODE)["error"] is None


def test_route_replay_prefers_complete_actual_cost_when_receipt_has_it():
    result = route_replay.score_candidate(route_replay.INITIAL_PROGRAM_CODE)
    named_test = result["cases"]["live_named_test"]
    assert named_test["dollar_source"] == "actual_usage"
    # Two reviewed complete receipts currently calibrate this case. The replay
    # evaluator deliberately uses their median so one cold-cache run cannot
    # dictate routing cost.
    assert named_test["dollars"] == pytest.approx(
        (0.03845155 + 0.01409415) / 2
    )
    assert result["cost_coverage"]["actual"] >= 1


def test_route_replay_live_supported_explicit_feature_route_improves_safely():
    seed = route_replay.score_candidate(route_replay.INITIAL_PROGRAM_CODE)
    holdout = route_replay.score_holdout(route_replay.INITIAL_PROGRAM_CODE)
    adversarial = route_replay.score_adversarial(route_replay.INITIAL_PROGRAM_CODE)
    broad = route_replay.INITIAL_PROGRAM_CODE.replace(
        '    if (\n        kind == "general"',
        '    if False and (\n        kind == "general"',
    )
    broad_result = route_replay.score_candidate(broad)
    search = seed
    assert search["error"] is None
    assert holdout["error"] is None
    assert adversarial["error"] is None
    assert search["score"] > broad_result["score"]
    assert search["cases"]["live_explicit_feature"]["route"] == "lean_explicit_feature"
    assert holdout["cases"]["bounded_explicit_feature"]["route"] == "lean_explicit_feature"
    assert holdout["cases"]["unknown_migration"]["route"] == "complete_general"
    assert adversarial["cases"]["explicit_security_change"]["route"] == "complete_general"


def test_portfolio_wide_local_scorecard_runs_every_promotion_gate():
    scorecard = local_scorecard()
    assert scorecard["schema"] == "ctx.alphaevolve-local-scorecard/v1"
    assert set(scorecard["experiments"]) == set(EXPERIMENTS)
    assert scorecard["all_gates_pass"] is True
    assert all(
        gate["passed"]
        for row in scorecard["experiments"].values()
        for gate in row["gates"].values()
    )
    fast_path = scorecard["experiments"]["naive-fast-path"]
    assert fast_path["candidate"] == "integrated"
    comparison = fast_path["gates"]["search"]["comparison"]
    assert comparison["baseline_over_candidate"]["dollars"] > 1.0
    assert comparison["baseline_over_candidate"]["visible_tokens"] > 1.0


def test_every_production_lever_is_registered_or_explicitly_protected():
    doc = registry_document()
    assert doc["schema"] == "ctx.alphaevolve-levers/v1"
    assert len(LEVERS) >= 20
    assert all(lever.experiment in EXPERIMENTS for lever in LEVERS if lever.mutable)
    protected = {lever.id for lever in LEVERS if not lever.mutable}
    assert protected == {
        "actual-usage-accounting",
        "secret-workspace-guards",
        "receipt-integrity",
    }
    assert all(len(experiment_fingerprint(name)) == 16 for name in EXPERIMENTS)


def test_readiness_promotion_and_shadow_reports_do_not_claim_deployment():
    readiness = readiness_report(("surface-policy", "guard-policy"))
    assert all(row["ready"] for row in readiness["experiments"].values())
    promotion = promotion_report(("surface-policy",))
    row = promotion["experiments"]["surface-policy"]
    assert row["production_promotion"] is False
    assert row["status"] == "managed_search_ready"
    shadow = shadow_report("surface-policy")
    assert shadow["mutated_production"] is False


def test_guard_optimizer_cannot_select_cheap_unsafe_allow():
    candidate = guard_policy.INITIAL_PROGRAM_CODE.replace(
        '    if state.get("secret") or state.get("outside_root"):',
        '    return "unsafe_allow"\n    if state.get("secret") or state.get("outside_root"): ',
    )
    result = guard_policy.score_candidate(candidate)
    assert result["score"] < 0
    assert "unsafe" in result["error"]


def test_execution_optimizer_cannot_reuse_a_stale_cache():
    candidate = execution_policy.INITIAL_PROGRAM_CODE.replace(
        '    if state.get("cache_valid"):',
        '    if state.get("cache_present"):\n        return "stale_reuse"\n    if state.get("cache_valid"): ',
    )
    result = execution_policy.score_adversarial(candidate)
    assert result["score"] < 0
    assert "same_size_stale_edit" in result["error"]


def test_shared_metric_treats_100x_as_a_multiplicative_gain():
    options = (
        choice_option(
            "naive", ("done",), dollars=100, visible_tokens=100,
            model_turns=100, tool_calls=100, latency_ms=100,
        ),
        choice_option(
            "candidate", ("done",), dollars=1, visible_tokens=1,
            model_turns=1, tool_calls=1, latency_ms=1,
        ),
    )
    cases = (choice_case("one", {}, ("done",), baseline="naive"),)
    code = """# EVOLVE-BLOCK-START
def choose(state, options):
    return "candidate"
# EVOLVE-BLOCK-END"""
    result = score_choice_policy(code, "choose", cases, options)
    assert result["error"] is None
    assert result["baseline_over_candidate"]["dollars"] == 100.0
    assert result["log2_gain"]["dollars"] == pytest.approx(9.965784, rel=1e-6)
