"""Freshness- and completion-gated execution policy evaluator."""

from pathlib import Path
from typing import Any
from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket execution backgrounding and cache policy"
METRIC_NAME = "completion_adjusted_execution_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_execution"
OPTIONS = (
    option("foreground_inline", ("result","fresh"), dollars=.015, visible_tokens=220, model_turns=1, tool_calls=1, latency_ms=40),
    option("foreground_captured", ("result","fresh","address"), dollars=.025, visible_tokens=420, model_turns=1, tool_calls=1, latency_ms=90),
    option("background_job", ("result","fresh","address","nonblocking"), dollars=.03, visible_tokens=260, model_turns=1, tool_calls=1, latency_ms=25),
    option("cache_reuse", ("result","fresh","cache_hit"), dollars=.002, visible_tokens=80, model_turns=1, tool_calls=0, latency_ms=3),
    option("rebuild", ("result","fresh","address"), dollars=.04, visible_tokens=380, model_turns=1, tool_calls=1, latency_ms=120),
    option("stale_reuse", ("result","cache_hit"), dollars=.001, visible_tokens=50, model_turns=1, tool_calls=0, latency_ms=1, safe=False),
)
SEARCH_CASES = (
    case("valid_cache", {"cache_present":True,"cache_valid":True}, ("result","fresh","cache_hit"), baseline="rebuild"),
    case("stale_cache", {"cache_present":True,"cache_valid":False}, ("result","fresh","address"), baseline="rebuild"),
    case("long_test", {"long_running":True}, ("result","fresh","address","nonblocking"), baseline="foreground_captured"),
    case("output_flood", {"flood":True}, ("result","fresh","address"), baseline="foreground_captured"),
    case("tiny_command", {}, ("result","fresh"), baseline="foreground_inline"),
)
HOLDOUT_CASES = (
    case("valid_map_cache", {"cache_present":True,"cache_valid":True}, ("result","fresh","cache_hit"), baseline="rebuild"),
    case("long_build", {"long_running":True,"flood":True}, ("result","fresh","address","nonblocking"), baseline="foreground_captured"),
    case("small_status", {}, ("result","fresh"), baseline="foreground_inline"),
)
ADVERSARIAL_CASES = (
    case("same_size_stale_edit", {"cache_present":True,"cache_valid":False,"same_size_mtime":True}, ("result","fresh","address"), baseline="rebuild", allowed=("rebuild",)),
    case("long_flood_must_address", {"long_running":True,"flood":True}, ("result","fresh","address","nonblocking"), baseline="foreground_captured", allowed=("background_job",)),
    case("tiny_command_not_backgrounded", {}, ("result","fresh"), baseline="foreground_inline", allowed=("foreground_inline","foreground_captured")),
)
def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]: return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
