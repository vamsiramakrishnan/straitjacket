"""Exact-classification evaluator for digest profile ordering."""

from pathlib import Path
from typing import Any
from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket digest-profile classifier"
METRIC_NAME = "completion_adjusted_profile_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_profile"

def _p(name: str, latency: int):
    return option(name, (f"profile:{name}",), dollars=.00001, visible_tokens=1, model_turns=1, tool_calls=1, latency_ms=latency)
OPTIONS = tuple(_p(name, latency) for name, latency in (("binary",1),("pytest",3),("lint",4),("search",5),("build",6),("json",7),("log",9),("text",10)))

def _c(name: str, state: dict[str, Any], expected: str):
    return case(name, state, (f"profile:{expected}",), baseline="text", allowed=(expected,))
SEARCH_CASES = (
    _c("pytest_failure", {"family":"pytest"}, "pytest"), _c("ruff_output", {"family":"lint"}, "lint"),
    _c("rg_rows", {"family":"search"}, "search"), _c("structured_log", {"family":"log"}, "log"),
    _c("json_document", {"family":"json"}, "json"), _c("unknown_prose", {}, "text"),
)
HOLDOUT_CASES = (
    _c("binary_pdf", {"magic_binary":True,"family":"text"}, "binary"),
    _c("compiler_build", {"family":"build"}, "build"), _c("json_lines", {"family":"json"}, "json"),
)
ADVERSARIAL_CASES = (
    _c("error_word_in_prose", {"family":"text","contains_error":True}, "text"),
    _c("binary_with_test_bytes", {"magic_binary":True,"family":"pytest"}, "binary"),
    _c("search_shaped_lint", {"family":"lint","file_line_rows":True}, "lint"),
)
def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]: return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
