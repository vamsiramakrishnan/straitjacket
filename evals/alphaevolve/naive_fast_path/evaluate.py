"""Completion-gated evaluator that requires beating naive simple-task handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from evals.alphaevolve.sandbox import (
    INVALID_SCORE,
    candidate_code,
    controller_evaluation,
    run_candidate,
)

TITLE = "straitjacket naive-use-case fast path"
METRIC_NAME = "completion_adjusted_naive_advantage"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
INTEGRATED_PROGRAM_CODE = (HERE / "integrated_program.py").read_text(
    encoding="utf-8"
)


def _plan(
    plan_id: str,
    capabilities: Iterable[str],
    *,
    visible_tokens: int,
    model_turns: int,
    tool_calls: int,
    dollars: float,
) -> dict[str, Any]:
    return {
        "id": plan_id,
        "capabilities": tuple(capabilities),
        "visible_tokens": visible_tokens,
        "model_turns": model_turns,
        "tool_calls": tool_calls,
        "dollars": dollars,
    }


PLANS: tuple[dict[str, Any], ...] = (
    _plan("cheap_noop", (), visible_tokens=200, model_turns=1, tool_calls=0, dollars=0.005),
    _plan("answer_given", ("answer",), visible_tokens=900, model_turns=1, tool_calls=0, dollars=0.015),
    _plan("focused_read", ("read", "answer"), visible_tokens=1600, model_turns=1, tool_calls=1, dollars=0.025),
    _plan("focused_diff", ("diff", "answer"), visible_tokens=1800, model_turns=1, tool_calls=1, dollars=0.030),
    _plan("focused_test", ("test",), visible_tokens=1200, model_turns=1, tool_calls=1, dollars=0.020),
    _plan("focused_edit_verify", ("read", "edit", "verify"), visible_tokens=2400, model_turns=1, tool_calls=2, dollars=0.045),
    _plan("focused_search_verify", ("search", "read", "edit", "verify"), visible_tokens=3800, model_turns=2, tool_calls=3, dollars=0.075),
    _plan(
        "broad_standard",
        ("answer", "search", "read", "diff", "edit", "verify", "test"),
        visible_tokens=18000,
        model_turns=3,
        tool_calls=5,
        dollars=0.250,
    ),
)


def _case(name: str, required: Iterable[str], **task: Any) -> dict[str, Any]:
    return {"name": name, "required": frozenset(required), "task": task}


SEARCH_CASES: tuple[dict[str, Any], ...] = (
    _case("explain_pasted_code", ("answer",), kind="explain", provided_context=True, target_known=True, mutation=False),
    _case("explain_named_symbol", ("read", "answer"), kind="explain", provided_context=False, target_known=True, mutation=False),
    _case("fix_known_typo", ("read", "edit", "verify"), kind="fix", provided_context=False, target_known=True, mutation=True),
    _case("run_named_test", ("test",), kind="test", provided_context=False, target_known=True, mutation=False),
    _case("summarize_diff", ("diff", "answer"), kind="review", changes_present=True, target_known=False, mutation=False),
    _case("edit_known_config", ("read", "edit", "verify"), kind="edit", provided_context=False, target_known=True, mutation=True),
    _case("diagnose_pasted_error", ("answer",), kind="diagnose", provided_context=True, failure_present=True, target_known=True, mutation=False),
    _case("inspect_known_file", ("read", "answer"), kind="inspect", provided_context=False, target_known=True, mutation=False),
)


HOLDOUT_CASES: tuple[dict[str, Any], ...] = (
    _case("answer_from_supplied_log", ("answer",), kind="diagnose", provided_context=True, failure_present=True, target_known=False, mutation=False),
    _case("update_known_doc", ("read", "edit", "verify"), kind="edit", provided_context=False, target_known=True, mutation=True),
    _case("review_existing_changes", ("diff", "answer"), kind="review", changes_present=True, target_known=True, mutation=False),
    _case("locate_small_bug", ("search", "read", "edit", "verify"), kind="fix", provided_context=False, failure_present=True, target_known=False, mutation=True),
)


ADVERSARIAL_CASES: tuple[dict[str, Any], ...] = (
    _case(
        "latest_release_from_supplied_context",
        ("answer",),
        kind="explain",
        provided_context=True,
        target_known=False,
        mutation=False,
        latest_release=True,
    ),
    _case(
        "summarize_customer_testimony",
        ("answer",),
        kind="summarize",
        provided_context=True,
        target_known=False,
        mutation=False,
        subject="customer testimony",
    ),
    _case(
        "unknown_read_only_lookup",
        ("search", "read", "answer"),
        kind="inspect",
        provided_context=False,
        target_known=False,
        mutation=False,
    ),
    _case(
        "review_changes_without_mutating",
        ("diff", "answer"),
        kind="review",
        changes_present=True,
        provided_context=False,
        target_known=True,
        mutation=False,
    ),
)


def _totals(plan_ids: list[str], plans_by_id: dict[str, dict[str, Any]]) -> dict[str, float]:
    return {
        key: sum(float(plans_by_id[plan_id][key]) for plan_id in plan_ids)
        for key in ("visible_tokens", "model_turns", "tool_calls", "dollars")
    }


def _score_cases(code: str, cases_to_score: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    calls = [{"task": case["task"], "plans": PLANS} for case in cases_to_score]
    run = run_candidate(code, "choose_fast_path", calls)
    if run["error"]:
        return {"score": INVALID_SCORE, "error": run["error"], "cases": {}}

    plans_by_id = {plan["id"]: plan for plan in PLANS}
    selected: list[str] = []
    failed: list[str] = []
    case_docs: dict[str, dict[str, Any]] = {}
    for case, plan_id in zip(cases_to_score, run["outputs"], strict=True):
        if not isinstance(plan_id, str) or plan_id not in plans_by_id:
            return {"score": INVALID_SCORE, "error": f"{case['name']}: invalid plan {plan_id!r}", "cases": {}}
        plan = plans_by_id[plan_id]
        complete = case["required"].issubset(set(plan["capabilities"]))
        if not complete:
            failed.append(case["name"])
        selected.append(plan_id)
        case_docs[case["name"]] = {
            "plan": plan_id,
            "completion": 1.0 if complete else 0.0,
            "visible_tokens": float(plan["visible_tokens"]),
            "model_turns": float(plan["model_turns"]),
            "tool_calls": float(plan["tool_calls"]),
            "dollars": float(plan["dollars"]),
        }

    if failed:
        return {
            "score": -100_000.0 - 1_000.0 * len(failed),
            "error": "incomplete naive tasks: " + ", ".join(failed),
            "cases": case_docs,
        }

    totals = _totals(selected, plans_by_id)
    baseline = _totals(["broad_standard"] * len(cases_to_score), plans_by_id)
    reductions = {key: 1.0 - totals[key] / baseline[key] for key in baseline}
    score = (
        100.0
        + 25.0 * reductions["dollars"]
        + 25.0 * reductions["visible_tokens"]
        + 20.0 * reductions["model_turns"]
        + 10.0 * reductions["tool_calls"]
    )
    pareto_beats_naive = all(totals[key] <= baseline[key] for key in baseline) and any(
        totals[key] < baseline[key] for key in baseline
    )
    return {
        "score": score,
        "error": None,
        "cases": case_docs,
        "totals": totals,
        "naive_direct_totals": baseline,
        "reductions": reductions,
        "pareto_beats_naive_direct": pareto_beats_naive,
        "naive_minimal_completion": 0.0,
    }


def score_candidate(code: str) -> dict[str, Any]:
    return _score_cases(code, SEARCH_CASES)


def score_holdout(code: str) -> dict[str, Any]:
    return _score_cases(code, HOLDOUT_CASES)


def score_adversarial(code: str) -> dict[str, Any]:
    return _score_cases(code, ADVERSARIAL_CASES)


def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        result = score_candidate(candidate_code(program_candidate))
    except (KeyError, IndexError, TypeError) as exc:
        result = {"score": INVALID_SCORE, "error": f"invalid envelope: {exc}"}
    detail = result.get("error") or (
        f"All naive tasks completed; score={result['score']:.3f}, "
        f"Pareto-beats broad baseline={result['pareto_beats_naive_direct']}"
    )
    return controller_evaluation(METRIC_NAME, result["score"], detail)
