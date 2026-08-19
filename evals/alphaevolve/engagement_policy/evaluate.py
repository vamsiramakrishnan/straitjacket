"""Behavioral-outcome evaluator for engagement and reflex policy."""

from pathlib import Path
from typing import Any
from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket graduated engagement and reflex"
METRIC_NAME = "completion_adjusted_engagement_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_engagement"
OPTIONS = (
    option("passive", ("evidence","address","quiet"), dollars=.01, visible_tokens=220, model_turns=1, tool_calls=1, latency_ms=5),
    option("active_lean", ("evidence","address","one_hint"), dollars=.014, visible_tokens=300, model_turns=1, tool_calls=1, latency_ms=6),
    option("active_full", ("evidence","address","guidance"), dollars=.022, visible_tokens=460, model_turns=1, tool_calls=1, latency_ms=7),
    option("dense", ("evidence","address","expanded_context"), dollars=.035, visible_tokens=850, model_turns=1, tool_calls=1, latency_ms=10),
    option("bypass", ("evidence","address","quiet","circuit_bypass"), dollars=.009, visible_tokens=180, model_turns=1, tool_calls=1, latency_ms=4),
)
SEARCH_CASES = (
    case("small_session", {"calls":2}, ("evidence","address","quiet"), baseline="active_full"),
    case("truncated_lean", {"truncated":True,"lean_model":True}, ("evidence","address","one_hint"), baseline="active_full"),
    case("truncated_frontier", {"truncated":True}, ("evidence","address","guidance"), baseline="active_full"),
    case("starvation", {"starved":True}, ("expanded_context","address"), baseline="active_full"),
    case("learned_positive", {"repeated_positive":True}, ("circuit_bypass","evidence","address"), baseline="active_full"),
)
HOLDOUT_CASES = (
    case("window_pressure", {"window_hot":True}, ("guidance","evidence"), baseline="active_full"),
    case("long_lean_session", {"calls":12,"lean_model":True}, ("one_hint","evidence"), baseline="active_full"),
    case("long_frontier_session", {"calls":12}, ("guidance","evidence"), baseline="active_full"),
)
ADVERSARIAL_CASES = (
    case("lean_starvation_still_dense", {"starved":True,"lean_model":True}, ("expanded_context","address"), baseline="active_full", allowed=("dense",)),
    case("positive_bypass_keeps_address", {"repeated_positive":True}, ("circuit_bypass","evidence","address"), baseline="active_full", allowed=("bypass",)),
    case("testimony_not_truncation", {"calls":1,"subject":"testimony"}, ("quiet","evidence"), baseline="active_full", allowed=("passive","bypass")),
)
def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]: return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
