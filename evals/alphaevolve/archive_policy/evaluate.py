"""Frozen evaluator for provider-aware, lossless cold-context routing."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import (
    case,
    controller_adapter,
    option,
    score_choice_policy,
)

TITLE = "straitjacket cold-context archive selector"
METRIC_NAME = "completion_adjusted_cold_context"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_archive_tier"

OPTIONS = (
    option("inline_text", ("exact", "recall", "structure"), dollars=.16, visible_tokens=32_000, model_turns=1, tool_calls=0, latency_ms=0),
    option("address_only", ("exact", "address"), dollars=.012, visible_tokens=900, model_turns=1, tool_calls=1, latency_ms=15),
    option("visual_cold", ("exact", "address", "recall", "structure"), dollars=.035, visible_tokens=2_800, model_turns=1, tool_calls=0, latency_ms=30),
)

SAFE_VISUAL = {
    "exact_evidence_secured": True,
    "exact_retrieval_address": True,
    "host_image_capable": True,
    "provider": "google",
    "at_compaction_boundary": True,
    "quiet_needle_gate": True,
    "structure_recall_gate": True,
    "contains_secrets": False,
    "provider_image_budget": 8,
    "estimated_frames": 4,
    "cold_tokens": 80_000,
}

SEARCH_CASES = (
    case("unsecured_history", {}, ("exact", "recall", "structure"), baseline="inline_text", allowed=("inline_text",)),
    case("safe_google_archive", SAFE_VISUAL, ("exact", "address", "recall", "structure"), baseline="inline_text", allowed=("visual_cold",)),
    case("small_addressed_history", {**SAFE_VISUAL, "cold_tokens": 8_000}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
    case("text_only_host", {**SAFE_VISUAL, "host_image_capable": False}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
    case("not_at_boundary", {**SAFE_VISUAL, "at_compaction_boundary": False}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
)

HOLDOUT_CASES = (
    case("safe_anthropic_archive", {**SAFE_VISUAL, "provider": "anthropic"}, ("exact", "address", "recall", "structure"), baseline="inline_text", allowed=("visual_cold",)),
    case("safe_openai_archive", {**SAFE_VISUAL, "provider": "openai"}, ("exact", "address", "recall", "structure"), baseline="inline_text", allowed=("visual_cold",)),
    case("addressed_medium_history", {**SAFE_VISUAL, "cold_tokens": 23_999}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
)

ADVERSARIAL_CASES = (
    case("quiet_needle_failed", {**SAFE_VISUAL, "quiet_needle_gate": False}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
    case("structure_failed", {**SAFE_VISUAL, "structure_recall_gate": False}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
    case("secret_history", {**SAFE_VISUAL, "contains_secrets": True}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
    case("unknown_provider", {**SAFE_VISUAL, "provider": "router-x"}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
    case("image_budget_overrun", {**SAFE_VISUAL, "provider_image_budget": 3}, ("exact", "address"), baseline="inline_text", allowed=("address_only",)),
    case("address_without_secured_bytes", {**SAFE_VISUAL, "exact_evidence_secured": False}, ("exact", "recall", "structure"), baseline="inline_text", allowed=("inline_text",)),
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
