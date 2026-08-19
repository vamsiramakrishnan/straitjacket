"""Deterministic evaluator for success-adjusted model routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.alphaevolve.sandbox import (
    INVALID_SCORE,
    candidate_code,
    controller_evaluation,
    run_candidate,
)

TITLE = "straitjacket success-adjusted execution routing"
METRIC_NAME = "completion_adjusted_route_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")

ROUTES: tuple[dict[str, Any], ...] = (
    {"id": "economy_single", "capability": 2, "context_window": 32000, "planning": False, "review": False, "visible_tokens": 18000, "repair_turns": 1.4, "dollars": 0.06, "latency": 1.0},
    {"id": "standard_single", "capability": 3, "context_window": 128000, "planning": False, "review": False, "visible_tokens": 35000, "repair_turns": 0.7, "dollars": 0.25, "latency": 2.0},
    {"id": "premium_single", "capability": 5, "context_window": 200000, "planning": False, "review": False, "visible_tokens": 50000, "repair_turns": 0.2, "dollars": 1.20, "latency": 4.0},
    {"id": "split_plan_build", "capability": 5, "context_window": 200000, "planning": True, "review": False, "visible_tokens": 42000, "repair_turns": 0.3, "dollars": 0.85, "latency": 3.1},
    {"id": "split_build_review", "capability": 5, "context_window": 200000, "planning": True, "review": True, "visible_tokens": 48000, "repair_turns": 0.15, "dollars": 1.10, "latency": 3.8},
    {"id": "lean_review", "capability": 3, "context_window": 128000, "planning": False, "review": True, "visible_tokens": 32000, "repair_turns": 0.5, "dollars": 0.45, "latency": 2.4},
)

CASES: tuple[dict[str, Any], ...] = (
    {"name": "one_line_typo", "task": {"complexity": 1, "risk": "low", "context_tokens": 4000, "needs_review": False, "latency_sensitive": True}, "completing": {"economy_single", "standard_single", "premium_single", "split_plan_build", "split_build_review", "lean_review"}},
    {"name": "small_known_bug", "task": {"complexity": 2, "risk": "medium", "context_tokens": 12000, "needs_review": False, "latency_sensitive": True}, "completing": {"economy_single", "standard_single", "premium_single", "split_plan_build", "split_build_review", "lean_review"}},
    {"name": "moderate_feature", "task": {"complexity": 3, "risk": "medium", "context_tokens": 45000, "needs_review": False, "latency_sensitive": False}, "completing": {"standard_single", "premium_single", "split_plan_build", "split_build_review", "lean_review"}},
    {"name": "large_context_refactor", "task": {"complexity": 3, "risk": "medium", "context_tokens": 110000, "needs_review": False, "latency_sensitive": False}, "completing": {"standard_single", "premium_single", "split_plan_build", "split_build_review", "lean_review"}},
    {"name": "architectural_change", "task": {"complexity": 4, "risk": "medium", "context_tokens": 90000, "needs_review": False, "latency_sensitive": False}, "completing": {"split_plan_build", "split_build_review"}},
    {"name": "authorization_boundary", "task": {"complexity": 4, "risk": "high", "context_tokens": 70000, "needs_review": True, "latency_sensitive": False}, "completing": {"split_build_review"}},
    {"name": "focused_security_review", "task": {"complexity": 3, "risk": "high", "context_tokens": 40000, "needs_review": True, "latency_sensitive": False}, "completing": {"lean_review", "split_build_review"}},
    {"name": "complex_migration", "task": {"complexity": 5, "risk": "high", "context_tokens": 160000, "needs_review": True, "latency_sensitive": False}, "completing": {"split_build_review"}},
)


HOLDOUT_CASES: tuple[dict[str, Any], ...] = (
    {"name": "tiny_known_lookup", "task": {"complexity": 1, "risk": "low", "context_tokens": 2000, "needs_review": False, "latency_sensitive": True}, "completing": {"economy_single", "standard_single", "premium_single", "split_plan_build", "split_build_review", "lean_review"}},
    {"name": "wide_standard_change", "task": {"complexity": 3, "risk": "medium", "context_tokens": 120000, "needs_review": False, "latency_sensitive": False}, "completing": {"standard_single", "premium_single", "split_plan_build", "split_build_review", "lean_review"}},
    {"name": "small_high_risk_review", "task": {"complexity": 2, "risk": "high", "context_tokens": 9000, "needs_review": True, "latency_sensitive": False}, "completing": {"lean_review", "split_build_review"}},
    {"name": "frontier_design", "task": {"complexity": 4, "risk": "medium", "context_tokens": 60000, "needs_review": False, "latency_sensitive": False}, "completing": {"split_plan_build", "split_build_review"}},
)


ADVERSARIAL_CASES: tuple[dict[str, Any], ...] = (
    {"name": "easy_words_high_risk", "task": {"complexity": 1, "risk": "high", "context_tokens": 4000, "needs_review": True, "latency_sensitive": True}, "completing": {"lean_review", "split_build_review"}},
    {"name": "context_exceeds_standard", "task": {"complexity": 3, "risk": "medium", "context_tokens": 190000, "needs_review": False, "latency_sensitive": False}, "completing": {"premium_single", "split_plan_build", "split_build_review"}},
    {"name": "explicit_review_overrides_low_risk", "task": {"complexity": 3, "risk": "low", "context_tokens": 20000, "needs_review": True, "latency_sensitive": True}, "completing": {"lean_review", "split_build_review"}},
    {"name": "maximum_complexity", "task": {"complexity": 5, "risk": "medium", "context_tokens": 100000, "needs_review": False, "latency_sensitive": True}, "completing": {"split_plan_build", "split_build_review"}},
)


def _score(code: str, cases_to_score: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    calls = [{"task": case["task"], "routes": ROUTES} for case in cases_to_score]
    run = run_candidate(code, "choose_route", calls)
    if run["error"]:
        return {"score": INVALID_SCORE, "error": run["error"], "cases": {}}
    route_by_id = {route["id"]: route for route in ROUTES}
    failures: list[str] = []
    cases: dict[str, dict[str, float | str]] = {}
    for case, route_id in zip(cases_to_score, run["outputs"], strict=True):
        if not isinstance(route_id, str) or route_id not in route_by_id:
            return {"score": INVALID_SCORE, "error": f"{case['name']}: invalid route {route_id!r}", "cases": {}}
        route = route_by_id[route_id]
        complete = route_id in case["completing"]
        if not complete:
            failures.append(case["name"])
        cases[case["name"]] = {
            "route": route_id,
            "completion": 1.0 if complete else 0.0,
            "dollars": float(route["dollars"]),
            "visible_tokens": float(route["visible_tokens"]),
            "repair_turns": float(route["repair_turns"]),
            "latency": float(route["latency"]),
        }
    if failures:
        return {
            "score": -100_000.0 - 1_000.0 * len(failures),
            "error": "non-completing routes: " + ", ".join(failures),
            "cases": cases,
        }
    dollars = sum(float(row["dollars"]) for row in cases.values())
    tokens = sum(float(row["visible_tokens"]) for row in cases.values())
    repairs = sum(float(row["repair_turns"]) for row in cases.values())
    latency = sum(float(row["latency"]) for row in cases.values())
    score = 150.0 - 5.0 * dollars - tokens / 25_000.0 - 1.5 * repairs - 0.2 * latency
    return {
        "score": score,
        "error": None,
        "cases": cases,
        "dollars": dollars,
        "visible_tokens": tokens,
        "repair_turns": repairs,
        "latency": latency,
    }


def score_candidate(code: str) -> dict[str, Any]:
    return _score(code, CASES)


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
        f"All tasks completed; dollars={result['dollars']:.3f}, "
        f"visible tokens={result['visible_tokens']:.0f}, repairs={result['repair_turns']:.2f}"
    )
    return controller_evaluation(METRIC_NAME, result["score"], detail)
