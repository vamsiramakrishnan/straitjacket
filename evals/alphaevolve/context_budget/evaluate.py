"""Deterministic evaluator for context-budget allocation."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from evals.alphaevolve.sandbox import (
    INVALID_SCORE,
    candidate_code,
    controller_evaluation,
    run_candidate,
)

TITLE = "straitjacket context-budget allocation"
METRIC_NAME = "completion_adjusted_context_value"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")


def _item(
    kind: str,
    tokens: int,
    utility: float,
    *,
    severity: int = 0,
    novelty: int = 1,
    addressable: bool = False,
    position: float = 0.5,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "tokens": tokens,
        "utility": utility,
        "severity": severity,
        "novelty": novelty,
        "addressable": addressable,
        "position": position,
    }


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "pytest_root_cause",
        "budget": 240,
        "items": [
            _item("command", 35, 2, position=0.0),
            _item("failure_identity", 45, 11, severity=2),
            _item("root_cause", 80, 18, severity=3),
            _item("source_coordinate", 35, 9, severity=2, addressable=True),
            _item("context", 100, 3, novelty=0),
            _item("retrieval_address", 25, 8, addressable=True, position=1.0),
            _item("teaching", 40, 1),
        ],
        "required": {1, 2, 3, 5},
    },
    {
        "name": "successful_verification",
        "budget": 150,
        "items": [
            _item("command", 28, 3, position=0.0),
            _item("context", 55, 2, novelty=0),
            _item("terminal_summary", 32, 11, position=1.0),
            _item("verification", 58, 17, severity=2),
            _item("teaching", 45, 1),
            _item("retrieval_address", 24, 6, addressable=True),
        ],
        "required": {2, 3, 5},
    },
    {
        "name": "compiler_diagnostic",
        "budget": 210,
        "items": [
            _item("command", 30, 2),
            _item("context", 75, 3, novelty=0),
            _item("root_cause", 70, 18, severity=3),
            _item("source_coordinate", 38, 10, severity=2, addressable=True),
            _item("context", 62, 7, severity=1),
            _item("terminal_summary", 35, 9, severity=2, position=1.0),
            _item("noise", 50, 0, novelty=0),
        ],
        "required": {2, 3, 5},
    },
    {
        "name": "failure_flood",
        "budget": 180,
        "items": [
            _item("failure_identity", 28, 8, severity=2),
            _item("failure_identity", 28, 8, severity=2),
            _item("failure_identity", 28, 8, severity=2),
            _item("root_cause", 62, 17, severity=3),
            _item("retrieval_address", 20, 8, addressable=True),
            _item("context", 90, 4),
            _item("teaching", 35, 1),
        ],
        "required": {0, 1, 2, 3, 4},
    },
    {
        "name": "search_target",
        "budget": 145,
        "items": [
            _item("command", 25, 2),
            _item("context", 40, 4),
            _item("source_coordinate", 30, 12, severity=1, addressable=True),
            _item("root_cause", 48, 14, severity=2),
            _item("context", 45, 3, novelty=0),
            _item("retrieval_address", 22, 8, addressable=True),
        ],
        "required": {2, 3, 5},
    },
    {
        "name": "config_parse_error",
        "budget": 170,
        "items": [
            _item("command", 26, 2),
            _item("root_cause", 54, 17, severity=3),
            _item("source_coordinate", 28, 10, severity=2, addressable=True),
            _item("context", 65, 5),
            _item("terminal_summary", 30, 9, severity=2),
            _item("retrieval_address", 20, 7, addressable=True),
            _item("noise", 35, 0, novelty=0),
        ],
        "required": {1, 2, 4, 5},
    },
)


HOLDOUT_CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "api_timeout",
        "budget": 165,
        "items": [
            _item("command", 30, 2),
            _item("failure_identity", 35, 10, severity=2),
            _item("root_cause", 55, 18, severity=3),
            _item("source_coordinate", 28, 9, severity=2, addressable=True),
            _item("retrieval_address", 18, 8, addressable=True),
            _item("noise", 80, 0, novelty=0),
        ],
        "required": {1, 2, 3, 4},
    },
    {
        "name": "schema_validation",
        "budget": 145,
        "items": [
            _item("verification", 48, 17, severity=2),
            _item("root_cause", 42, 16, severity=3),
            _item("terminal_summary", 28, 10, position=1.0),
            _item("retrieval_address", 20, 7, addressable=True),
            _item("teaching", 60, 2),
        ],
        "required": {0, 1, 2, 3},
    },
    {
        "name": "repeated_noise",
        "budget": 180,
        "items": [
            _item("failure_identity", 25, 8, severity=1),
            _item("failure_identity", 25, 8, severity=1, novelty=0),
            _item("root_cause", 58, 18, severity=3),
            _item("source_coordinate", 30, 10, addressable=True),
            _item("retrieval_address", 20, 8, addressable=True),
            _item("context", 65, 4, novelty=0),
        ],
        "required": {0, 2, 3, 4},
    },
)


ADVERSARIAL_CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "benign_no_errors_phrase",
        "budget": 125,
        "items": [
            _item("context", 45, 5, severity=2),
            _item("verification", 48, 18, severity=2),
            _item("terminal_summary", 28, 11, position=1.0),
            _item("retrieval_address", 20, 7, addressable=True),
            _item("noise", 40, 0),
        ],
        "required": {1, 2, 3},
    },
    {
        "name": "high_severity_noise",
        "budget": 135,
        "items": [
            _item("noise", 70, 0, severity=3, novelty=0),
            _item("root_cause", 58, 18, severity=3),
            _item("source_coordinate", 30, 10, severity=2, addressable=True),
            _item("retrieval_address", 20, 8, addressable=True),
            _item("teaching", 40, 1),
        ],
        "required": {1, 2, 3},
    },
    {
        "name": "exact_mandatory_budget",
        "budget": 110,
        "items": [
            _item("root_cause", 52, 18, severity=3),
            _item("source_coordinate", 30, 10, severity=2, addressable=True),
            _item("retrieval_address", 18, 8, addressable=True),
            _item("terminal_summary", 10, 9, position=1.0),
            _item("command", 30, 2),
        ],
        "required": {0, 1, 2, 3},
    },
)


def _optimal_utility(items: list[dict[str, Any]], budget: int) -> float:
    best = 0.0
    for count in range(len(items) + 1):
        for indices in itertools.combinations(range(len(items)), count):
            if sum(items[i]["tokens"] for i in indices) <= budget:
                best = max(best, sum(items[i]["utility"] for i in indices))
    return best


def _score(code: str, cases_to_score: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    calls = [
        {"items": tuple(case["items"]), "budget_tokens": case["budget"]}
        for case in cases_to_score
    ]
    run = run_candidate(code, "allocate_context", calls)
    if run["error"]:
        return {"score": INVALID_SCORE, "error": run["error"], "cases": {}}

    case_docs: dict[str, dict[str, float]] = {}
    missing_total = 0
    for case, output in zip(cases_to_score, run["outputs"], strict=True):
        if not isinstance(output, list) or any(type(i) is not int for i in output):
            return {"score": INVALID_SCORE, "error": f"{case['name']}: expected list[int]", "cases": {}}
        if output != sorted(set(output)):
            return {"score": INVALID_SCORE, "error": f"{case['name']}: indices not unique/ordered", "cases": {}}
        if any(i < 0 or i >= len(case["items"]) for i in output):
            return {"score": INVALID_SCORE, "error": f"{case['name']}: invalid index", "cases": {}}
        spent = sum(case["items"][i]["tokens"] for i in output)
        if spent > case["budget"]:
            return {"score": INVALID_SCORE, "error": f"{case['name']}: token budget exceeded", "cases": {}}
        missing = len(case["required"] - set(output))
        missing_total += missing
        utility = sum(case["items"][i]["utility"] for i in output)
        optimal = _optimal_utility(case["items"], case["budget"])
        case_docs[case["name"]] = {
            "completion": 1.0 if missing == 0 else 0.0,
            "utility_ratio": utility / optimal if optimal else 1.0,
            "budget_ratio": spent / case["budget"],
        }

    if missing_total:
        return {
            "score": -100_000.0 - 1_000.0 * missing_total,
            "error": f"missing {missing_total} mandatory evidence item(s)",
            "cases": case_docs,
        }
    mean_utility = sum(row["utility_ratio"] for row in case_docs.values()) / len(case_docs)
    mean_budget = sum(row["budget_ratio"] for row in case_docs.values()) / len(case_docs)
    score = 100.0 * mean_utility + 12.0 * (1.0 - mean_budget)
    return {
        "score": score,
        "error": None,
        "cases": case_docs,
        "elapsed_ms": run["elapsed_ms"],
        "mean_budget_ratio": mean_budget,
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
        f"All completion gates passed; value={result['score']:.3f}, "
        f"visible-budget ratio={result['mean_budget_ratio']:.3f}"
    )
    return controller_evaluation(METRIC_NAME, result["score"], detail)
