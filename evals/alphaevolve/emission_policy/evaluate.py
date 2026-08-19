"""Evidence-complete output-emission evaluator."""

from pathlib import Path
from typing import Any
from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket output delivery policy"
METRIC_NAME = "completion_adjusted_emission_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_emission"
OPTIONS = (
    option("raw_small", ("complete_output",), dollars=.001, visible_tokens=80, model_turns=1, tool_calls=0, latency_ms=2),
    option("raw_flood", ("complete_output",), dollars=.80, visible_tokens=120000, model_turns=4, tool_calls=1, latency_ms=500),
    option("standard_digest", ("summary", "address", "decisive_fact"), dollars=.035, visible_tokens=480, model_turns=2, tool_calls=1, latency_ms=35),
    option("dense_digest", ("summary", "address", "decisive_fact", "expanded_context"), dollars=.055, visible_tokens=900, model_turns=1, tool_calls=1, latency_ms=45),
    option("typed_failure", ("summary", "address", "decisive_fact", "failure_census"), dollars=.045, visible_tokens=760, model_turns=2, tool_calls=1, latency_ms=42),
)
SEARCH_CASES = (
    case("tiny_success", {"small":True}, ("complete_output",), baseline="raw_small", allowed=("raw_small",)),
    case("large_success", {"small":False}, ("summary","address","decisive_fact"), baseline="raw_flood"),
    case("pytest_failure", {"failure":True}, ("failure_census","address","decisive_fact"), baseline="raw_flood"),
    case("derived_small_result", {"small":True,"derived_evidence":True}, ("summary","address","decisive_fact"), baseline="raw_flood"),
    case("starved_rerun", {"starved":True}, ("expanded_context","address","decisive_fact"), baseline="raw_flood"),
)
HOLDOUT_CASES = (
    case("compiler_failure", {"failure":True}, ("failure_census","address","decisive_fact"), baseline="raw_flood"),
    case("quiet_log_flood", {}, ("summary","address","decisive_fact"), baseline="raw_flood"),
    case("two_line_success", {"small":True}, ("complete_output",), baseline="raw_small", allowed=("raw_small",)),
)
ADVERSARIAL_CASES = (
    case("small_but_semantic", {"small":True,"derived_evidence":True}, ("summary","address","decisive_fact"), baseline="raw_flood", allowed=("standard_digest","dense_digest","typed_failure")),
    case("failure_tempted_by_short_digest", {"failure":True}, ("failure_census","address","decisive_fact"), baseline="raw_flood", allowed=("typed_failure",)),
    case("flood_cannot_raw_inline", {}, ("summary","address"), baseline="raw_flood", allowed=("standard_digest","dense_digest","typed_failure")),
)
def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]: return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
