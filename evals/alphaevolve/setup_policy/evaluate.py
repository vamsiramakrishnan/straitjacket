"""Completion-gated evaluator for setup and repair DevEx."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import (
    case,
    controller_adapter,
    option,
    score_choice_policy,
)

TITLE = "straitjacket setup and repair DevEx"
METRIC_NAME = "completion_adjusted_setup_friction"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
INTEGRATED_PROGRAM_CODE = (
    HERE.parents[2] / "src" / "ctx" / "setup_policy.py"
).read_text(encoding="utf-8")
FUNCTION_NAME = "choose_setup"

OPTIONS = (
    option(
        "ready_noop",
        ("complete", "verified", "idempotent", "quiet", "preserves_user"),
        dollars=0.0001,
        visible_tokens=28,
        model_turns=0,
        tool_calls=0,
        latency_ms=2,
    ),
    option(
        "configure_detected",
        ("complete", "verified", "detected_only", "preserves_user"),
        dollars=0.002,
        visible_tokens=260,
        model_turns=0,
        tool_calls=1,
        latency_ms=35,
    ),
    option(
        "configure_explicit",
        ("complete", "verified", "explicit_scope", "preserves_user"),
        dollars=0.002,
        visible_tokens=240,
        model_turns=0,
        tool_calls=1,
        latency_ms=35,
    ),
    option(
        "configure_all",
        ("complete", "verified", "future_ready", "preserves_user"),
        dollars=0.003,
        visible_tokens=420,
        model_turns=0,
        tool_calls=1,
        latency_ms=55,
    ),
    option(
        "repair_managed",
        ("complete", "verified", "repair", "diagnosis", "preserves_user"),
        dollars=0.003,
        visible_tokens=360,
        model_turns=0,
        tool_calls=1,
        latency_ms=55,
    ),
    option(
        "refuse_unmanaged",
        ("safe_refusal", "diagnosis", "preserves_user"),
        dollars=0.001,
        visible_tokens=120,
        model_turns=0,
        tool_calls=0,
        latency_ms=4,
    ),
    # The pre-optimization repeat path: rerun all installers and print the full
    # four-step narrative even though the verified managed state is unchanged.
    option(
        "naive_repeat",
        ("complete", "verified", "preserves_user"),
        dollars=0.004,
        visible_tokens=680,
        model_turns=0,
        tool_calls=1,
        latency_ms=65,
    ),
)

SEARCH_CASES = (
    case(
        "repeat_current",
        {"receipt_current": True, "installed_hosts": ["codex"]},
        ("complete", "verified", "idempotent", "quiet", "preserves_user"),
        baseline="naive_repeat",
        allowed=("ready_noop",),
    ),
    case(
        "fresh_detected",
        {"installed_hosts": ["codex", "claude"]},
        ("complete", "verified", "detected_only", "preserves_user"),
        baseline="configure_all",
        allowed=("configure_detected",),
    ),
    case(
        "fresh_no_host",
        {"installed_hosts": []},
        ("complete", "verified", "future_ready", "preserves_user"),
        baseline="configure_all",
        allowed=("configure_all",),
    ),
    case(
        "managed_drift",
        {"had_receipt": True, "installed_hosts": ["codex"]},
        ("complete", "verified", "repair", "diagnosis", "preserves_user"),
        baseline="naive_repeat",
        allowed=("repair_managed",),
    ),
)

HOLDOUT_CASES = (
    case(
        "explicit_host",
        {"explicit": True, "installed_hosts": []},
        ("complete", "verified", "explicit_scope", "preserves_user"),
        baseline="configure_all",
        allowed=("configure_explicit",),
    ),
    case(
        "forced_repair",
        {"receipt_current": True, "force_repair": True, "had_receipt": True},
        ("complete", "verified", "repair", "preserves_user"),
        baseline="naive_repeat",
        allowed=("repair_managed",),
    ),
)

ADVERSARIAL_CASES = (
    case(
        "unmanaged_conflict",
        {"unmanaged_conflict": True, "installed_hosts": ["codex"]},
        ("safe_refusal", "diagnosis", "preserves_user"),
        baseline="refuse_unmanaged",
        allowed=("refuse_unmanaged",),
    ),
    case(
        "stale_receipt_not_noop",
        {"receipt_current": False, "had_receipt": True},
        ("complete", "verified", "repair", "preserves_user"),
        baseline="naive_repeat",
        allowed=("repair_managed",),
    ),
)


def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)


def score_candidate(code: str) -> dict[str, Any]:
    return _score(code, SEARCH_CASES)


def score_holdout(code: str) -> dict[str, Any]:
    return _score(code, HOLDOUT_CASES)


def score_adversarial(code: str) -> dict[str, Any]:
    return _score(code, ADVERSARIAL_CASES)


def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    return controller_adapter(
        program_candidate,
        metric_name=METRIC_NAME,
        function_name=FUNCTION_NAME,
        cases=SEARCH_CASES,
        options=OPTIONS,
    )
