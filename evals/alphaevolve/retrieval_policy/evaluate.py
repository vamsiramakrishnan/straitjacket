"""Completion-gated retrieval-policy evaluator."""

from pathlib import Path
from typing import Any
from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket retrieval strategy and budget"
METRIC_NAME = "completion_adjusted_retrieval_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_retrieval"
OPTIONS = (
    option("raw_get", ("context",), dollars=.12, visible_tokens=12000, model_turns=2, tool_calls=2, latency_ms=180),
    option("exact_span", ("context","exact_target"), dollars=.012, visible_tokens=420, model_turns=1, tool_calls=1, latency_ms=20),
    option("refs_then_span", ("context","exact_target","callers"), dollars=.025, visible_tokens=900, model_turns=1, tool_calls=2, latency_ms=45),
    option("fails_then_span", ("context","exact_target","root_cause"), dollars=.028, visible_tokens=980, model_turns=1, tool_calls=2, latency_ms=50),
    option("map_then_get", ("context","exact_target","repo_structure"), dollars=.045, visible_tokens=1600, model_turns=2, tool_calls=2, latency_ms=80),
    option("focused_search", ("context","exact_target"), dollars=.035, visible_tokens=1300, model_turns=2, tool_calls=2, latency_ms=65),
    option("dry_repeat", (), dollars=.04, visible_tokens=400, model_turns=3, tool_calls=3, latency_ms=100),
)
SEARCH_CASES = (
    case("addressed_omission", {"address":True}, ("context","exact_target"), baseline="raw_get"),
    case("named_symbol", {"symbol":True}, ("context","exact_target","callers"), baseline="raw_get"),
    case("failed_test", {"failure":True}, ("context","exact_target","root_cause"), baseline="raw_get"),
    case("unknown_architecture", {"unknown_scope":True}, ("context","exact_target","repo_structure"), baseline="raw_get"),
    case("known_phrase", {}, ("context","exact_target"), baseline="raw_get"),
)
HOLDOUT_CASES = (
    case("log_span", {"address":True,"failure":True}, ("context","exact_target"), baseline="raw_get"),
    case("api_callers", {"symbol":True}, ("context","callers"), baseline="raw_get"),
    case("unfamiliar_repo", {"unknown_scope":True}, ("repo_structure","exact_target","context"), baseline="raw_get"),
)
ADVERSARIAL_CASES = (
    case("dry_is_not_cheap_success", {"dry_prior":True}, ("context","exact_target"), baseline="raw_get", allowed=("focused_search","map_then_get")),
    case("address_beats_search", {"address":True,"unknown_scope":True}, ("context","exact_target"), baseline="raw_get", allowed=("exact_span",)),
    case("failure_needs_root_cause", {"failure":True,"symbol":True}, ("root_cause","context"), baseline="raw_get", allowed=("fails_then_span",)),
)
def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]: return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
