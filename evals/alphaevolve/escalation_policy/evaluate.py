"""Completion-gated evaluator for retry/retrieve/replan/escalate decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from evals.alphaevolve.sandbox import (
    INVALID_SCORE,
    candidate_code,
    controller_evaluation,
    run_candidate,
)

TITLE = "straitjacket completion-adjusted escalation policy"
METRIC_NAME = "completion_adjusted_recovery_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")


def _action(action_id: str, cost: float, turns: int, latency_ms: int) -> dict[str, Any]:
    return {
        "id": action_id,
        "cost_usd": cost,
        "model_attempts": turns,
        "latency_ms": latency_ms,
    }


ACTIONS = (
    _action("focused_retrieve", 0.006, 0, 500),
    _action("retry_same", 0.045, 1, 9000),
    _action("replan", 0.075, 1, 12000),
    _action("escalate", 0.240, 1, 26000),
    _action("stop_blocked", 0.0, 0, 50),
    _action("stop_budget", 0.0, 0, 50),
)


def _case(
    name: str,
    failure_kind: str,
    accepted: Iterable[str],
    *,
    attempts: int = 1,
    budget: float = 1.0,
) -> dict[str, Any]:
    return {
        "name": name,
        "state": {
            "failure_kind": failure_kind,
            "attempts": attempts,
            "budget_remaining_usd": budget,
            "actions": ACTIONS,
        },
        "accepted": frozenset(accepted),
    }


SEARCH_CASES = (
    _case("missing_log_span", "missing_evidence", ("focused_retrieve",)),
    _case("digest_omission", "context_omission", ("focused_retrieve",)),
    _case("underspecified_task", "incomplete_contract", ("replan",)),
    _case("mutation_test_failed", "verification_failure", ("replan",)),
    _case("first_network_reset", "transient_transport", ("retry_same",)),
    _case("weak_model_limit", "capability_limit", ("escalate",)),
    _case("permission_prompt", "permission_denied", ("stop_blocked",)),
    _case("expired_login", "auth_failure", ("stop_blocked",)),
)

HOLDOUT_CASES = (
    _case("provider_rate_limit", "rate_limited", ("retry_same",)),
    _case("second_incomplete_answer", "repeated_incomplete", ("escalate",), attempts=2),
    _case("policy_gate", "safety_denied", ("stop_blocked",)),
    _case("no_escalation_budget", "capability_limit", ("stop_budget",), budget=0.05),
)

ADVERSARIAL_CASES = (
    _case("permission_words_are_not_permission_kind", "capability_limit", ("escalate",)),
    _case("zero_exit_explicit_incomplete", "incomplete_contract", ("replan",)),
    _case("repeat_transport_should_not_loop", "transient_transport", ("replan",), attempts=2),
    _case("auth_cannot_be_fixed_by_opus", "auth_failure", ("stop_blocked",)),
)


def _score(code: str, cases: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    run = run_candidate(
        code,
        "choose_recovery",
        [{"state": case["state"]} for case in cases],
    )
    if run["error"]:
        return {"score": INVALID_SCORE, "error": run["error"], "cases": {}}
    actions = {action["id"]: action for action in ACTIONS}
    failed: list[str] = []
    details: dict[str, dict[str, Any]] = {}
    for case, selected in zip(cases, run["outputs"], strict=True):
        valid = isinstance(selected, str) and selected in actions
        within_budget = valid and actions[selected]["cost_usd"] <= case["state"]["budget_remaining_usd"]
        completed = bool(within_budget and selected in case["accepted"])
        if not completed:
            failed.append(case["name"])
        action = actions.get(selected, {"cost_usd": 0.0, "model_attempts": 0, "latency_ms": 0})
        details[case["name"]] = {
            "completion": 1.0 if completed else 0.0,
            "action": selected,
            "cost_usd": float(action["cost_usd"]),
            "model_attempts": float(action["model_attempts"]),
            "latency_ms": float(action["latency_ms"]),
        }
    if failed:
        return {
            "score": -100_000.0 - 1_000.0 * len(failed),
            "error": "incorrect recovery: " + ", ".join(failed),
            "cases": details,
        }
    mean_cost = sum(row["cost_usd"] for row in details.values()) / len(details)
    mean_attempts = sum(row["model_attempts"] for row in details.values()) / len(details)
    mean_latency = sum(row["latency_ms"] for row in details.values()) / len(details)
    return {
        "score": 130.0 - 120.0 * mean_cost - 8.0 * mean_attempts - mean_latency / 5000.0,
        "error": None,
        "cases": details,
        "mean_cost_usd": mean_cost,
        "mean_model_attempts": mean_attempts,
        "mean_latency_ms": mean_latency,
    }


def score_candidate(code: str) -> dict[str, Any]:
    return _score(code, SEARCH_CASES)


def score_holdout(code: str) -> dict[str, Any]:
    return _score(code, HOLDOUT_CASES)


def score_adversarial(code: str) -> dict[str, Any]:
    return _score(code, ADVERSARIAL_CASES)


def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        result = score_candidate(candidate_code(program_candidate))
    except (KeyError, IndexError, TypeError) as exc:
        result = {"score": INVALID_SCORE, "error": f"invalid envelope: {exc}"}
    detail = result.get("error") or (
        f"All recoveries correct; mean cost=${result['mean_cost_usd']:.4f}, "
        f"attempts={result['mean_model_attempts']:.3f}"
    )
    return controller_evaluation(METRIC_NAME, result["score"], detail)
