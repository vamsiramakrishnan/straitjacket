"""Completion-gated plan compilation evaluator."""

from pathlib import Path
from typing import Any
from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket evidence-plan compiler"
METRIC_NAME = "completion_adjusted_plan_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_plan"
OPTIONS = (
    option("direct", ("answer",), dollars=.015, visible_tokens=700, model_turns=1, tool_calls=0, latency_ms=80),
    option("refs_context", ("target","callers","context"), dollars=.035, visible_tokens=1200, model_turns=1, tool_calls=2, latency_ms=55),
    option("diagnose_join", ("root_cause","target","context","verification"), dollars=.05, visible_tokens=1700, model_turns=2, tool_calls=3, latency_ms=90),
    option("diff_verify", ("changes","risk","verification"), dollars=.04, visible_tokens=1400, model_turns=2, tool_calls=2, latency_ms=75),
    option("bounded_investigate", ("repo_structure","target","context","verification"), dollars=.10, visible_tokens=3200, model_turns=3, tool_calls=4, latency_ms=180),
    option("broad_shell", ("repo_structure","target","context","verification"), dollars=.35, visible_tokens=18000, model_turns=5, tool_calls=8, latency_ms=600, safe=False),
)
SEARCH_CASES = (
    case("supplied_context", {"provided_context":True}, ("answer",), baseline="bounded_investigate"),
    case("failing_test", {"failure":True}, ("root_cause","target","context","verification"), baseline="bounded_investigate"),
    case("named_symbol", {"symbol":True}, ("target","callers","context"), baseline="bounded_investigate"),
    case("review_diff", {"changes":True}, ("changes","risk","verification"), baseline="bounded_investigate"),
    case("unknown_task", {}, ("repo_structure","target","context","verification"), baseline="bounded_investigate"),
)
HOLDOUT_CASES = (
    case("named_api", {"symbol":True}, ("target","callers","context"), baseline="bounded_investigate"),
    case("runtime_failure", {"failure":True}, ("root_cause","target","verification"), baseline="bounded_investigate"),
    case("architecture_change", {}, ("repo_structure","target","context","verification"), baseline="bounded_investigate"),
)
ADVERSARIAL_CASES = (
    case("cheap_shell_forbidden", {}, ("repo_structure","target","context","verification"), baseline="bounded_investigate", allowed=("bounded_investigate",)),
    case("testimony_not_failure", {"subject":"testimony"}, ("repo_structure","target","context","verification"), baseline="bounded_investigate", allowed=("bounded_investigate",)),
    case("provided_context_no_scan", {"provided_context":True,"symbol":True}, ("answer",), baseline="bounded_investigate", allowed=("direct",)),
)
def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]: return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
