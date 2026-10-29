"""Completion- and independence-gated evaluator for harness routing."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket cross-harness task routing"
METRIC_NAME = "completion_adjusted_routing_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_harness"

OPTIONS = (
    # id, provides, dollars, visible_tokens, model_turns, tool_calls, latency_ms
    option("codex", ("complete", "mutation_capable", "review_capable", "unattended_capable"), dollars=1.0, visible_tokens=14000, model_turns=6, tool_calls=14, latency_ms=420),
    option("claude", ("complete", "mutation_capable", "review_capable", "unattended_capable"), dollars=1.4, visible_tokens=16000, model_turns=7, tool_calls=15, latency_ms=480),
    option("gemini", ("complete", "review_capable", "unattended_capable"), dollars=0.35, visible_tokens=9000, model_turns=4, tool_calls=9, latency_ms=210),
    option("agy", ("complete", "frontier_reasoning"), dollars=0.5, visible_tokens=8000, model_turns=3, tool_calls=6, latency_ms=180),
    option("coordinator", ("complete", "secret_safe"), dollars=2.2, visible_tokens=26000, model_turns=10, tool_calls=20, latency_ms=900),
)

SEARCH_CASES = (
    case(
        "implement_mutation",
        {"task_kind": "implement", "shape_known": True},
        ("complete", "mutation_capable"),
        baseline="coordinator",
        allowed=("codex", "claude", "coordinator"),
    ),
    case(
        "docs_bulk_summarize",
        {"task_kind": "docs", "shape_known": True},
        ("complete",),
        baseline="coordinator",
        allowed=("gemini", "codex", "claude", "coordinator"),
    ),
    case(
        "unknown_shape_stays_local",
        {"task_kind": "unknown", "shape_known": False},
        ("complete",),
        baseline="coordinator",
        allowed=("coordinator",),
    ),
    case(
        "cross_review_independent_of_codex_author",
        {"task_kind": "review", "shape_known": True, "author_host": "codex"},
        ("complete", "review_capable"),
        baseline="coordinator",
        allowed=("gemini", "claude", "coordinator"),
    ),
    case(
        "user_pin_is_authoritative",
        {"task_kind": "implement", "shape_known": True, "pinned": "claude"},
        ("complete", "mutation_capable"),
        baseline="coordinator",
        allowed=("claude",),
    ),
)

HOLDOUT_CASES = (
    case(
        "cross_review_independent_of_claude_author",
        {"task_kind": "review", "shape_known": True, "author_host": "claude"},
        ("complete", "review_capable"),
        baseline="coordinator",
        allowed=("gemini", "codex", "coordinator"),
    ),
    case(
        "triage_small_read",
        {"task_kind": "triage", "shape_known": True},
        ("complete",),
        baseline="coordinator",
        allowed=("gemini", "codex", "coordinator"),
    ),
)

ADVERSARIAL_CASES = (
    case(
        "secret_material_never_leaves_coordinator",
        {"task_kind": "docs", "shape_known": True, "secret_material": True},
        ("complete",),
        baseline="coordinator",
        allowed=("coordinator",),
    ),
    case(
        "unattended_nightly_needs_headless_host",
        {"task_kind": "docs", "shape_known": True, "requires_unattended": True},
        ("complete", "unattended_capable"),
        baseline="coordinator",
        allowed=("gemini", "codex", "claude", "coordinator"),
    ),
    case(
        "interactive_frontier_task_cannot_be_unattended",
        {"task_kind": "implement", "shape_known": True, "requires_unattended": True},
        ("complete", "mutation_capable", "unattended_capable"),
        baseline="coordinator",
        allowed=("codex", "claude", "coordinator"),
    ),
)


def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)


def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    return controller_adapter(program_candidate, metric_name=METRIC_NAME,
                              function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
