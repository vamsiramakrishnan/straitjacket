"""Completion-gated capability-surface evaluator."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket capability-surface compiler"
METRIC_NAME = "completion_adjusted_surface_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_surface"

OPTIONS = (
    option("full", ("answer", "read", "search", "diff", "edit", "test", "deploy", "authorize"), dollars=.44, visible_tokens=12268, model_turns=4, tool_calls=5, latency_ms=900),
    option("local_dev", ("answer", "read", "search", "edit", "test"), dollars=.18, visible_tokens=7500, model_turns=3, tool_calls=4, latency_ms=560),
    option("review", ("answer", "read", "diff", "test"), dollars=.13, visible_tokens=5600, model_turns=2, tool_calls=3, latency_ms=410),
    option("read_only", ("answer", "read", "search"), dollars=.09, visible_tokens=4200, model_turns=2, tool_calls=2, latency_ms=300),
    option("minimal_answer", ("answer",), dollars=.025, visible_tokens=1800, model_turns=1, tool_calls=0, latency_ms=120),
)

SEARCH_CASES = (
    case("supplied_answer", {"provided_context": True, "phase": "explore"}, ("answer",), baseline="full"),
    case("known_lookup", {"phase": "explore"}, ("read", "search", "answer"), baseline="full"),
    case("small_edit", {"phase": "edit", "mutation": True}, ("read", "edit", "test"), baseline="full"),
    case("diff_review", {"phase": "review"}, ("diff", "read", "answer"), baseline="full"),
)
HOLDOUT_CASES = (
    case("pasted_failure", {"provided_context": True}, ("answer",), baseline="full"),
    case("local_feature", {"phase": "edit", "mutation": True}, ("search", "read", "edit", "test"), baseline="full"),
    case("release_review", {"phase": "review"}, ("diff", "test", "answer"), baseline="full"),
)
ADVERSARIAL_CASES = (
    case("unknown_request", {"unknown": True}, ("search", "read", "edit", "test"), baseline="full", allowed=("full",)),
    case("security_deploy", {"high_risk": True, "mutation": True}, ("edit", "test", "deploy", "authorize"), baseline="full", allowed=("full",)),
    case("testimony_answer", {"provided_context": True, "subject": "testimony"}, ("answer",), baseline="full"),
)

def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
