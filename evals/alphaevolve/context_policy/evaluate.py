"""Relevant-context-per-token evaluator."""

from pathlib import Path
from typing import Any
from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket repository-context selector"
METRIC_NAME = "completion_adjusted_repository_context"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_context"
OPTIONS = (
    option("named_file", ("target","context"), dollars=.012, visible_tokens=700, model_turns=1, tool_calls=1, latency_ms=15),
    option("symbol_neighborhood", ("target","context","dependencies","callers"), dollars=.025, visible_tokens=1200, model_turns=1, tool_calls=2, latency_ms=35),
    option("changed_files", ("target","context","dependencies","changes"), dollars=.03, visible_tokens=1500, model_turns=1, tool_calls=2, latency_ms=40),
    option("scoped_corpus", ("target","context","dependencies","repo_structure"), dollars=.05, visible_tokens=2600, model_turns=2, tool_calls=2, latency_ms=70),
    option("full_repo", ("target","context","dependencies","callers","changes","repo_structure"), dollars=.18, visible_tokens=9500, model_turns=3, tool_calls=4, latency_ms=220),
)
SEARCH_CASES = (
    case("known_file", {"named_file":True}, ("target","context"), baseline="full_repo"),
    case("named_function", {"symbol":True}, ("target","context","callers"), baseline="full_repo"),
    case("review_changes", {"changes":True}, ("changes","target","dependencies"), baseline="full_repo"),
    case("python_subsystem", {"language":"python"}, ("repo_structure","target","dependencies"), baseline="full_repo"),
    case("unknown_architecture", {}, ("repo_structure","target","dependencies","callers"), baseline="full_repo"),
)
HOLDOUT_CASES = (
    case("known_config", {"named_file":True}, ("target","context"), baseline="full_repo"),
    case("api_symbol", {"symbol":True}, ("target","callers","dependencies"), baseline="full_repo"),
    case("rust_crate", {"language":"rust"}, ("repo_structure","target","dependencies"), baseline="full_repo"),
)
ADVERSARIAL_CASES = (
    case("named_file_with_architecture_scope", {"named_file":True,"unknown_architecture":True}, ("repo_structure","target","dependencies","callers"), baseline="full_repo", allowed=("full_repo",)),
    case("changes_require_dependencies", {"changes":True}, ("changes","dependencies","target"), baseline="full_repo", allowed=("changed_files","full_repo")),
    case("unknown_cannot_guess_language", {}, ("repo_structure","target","dependencies","callers"), baseline="full_repo", allowed=("full_repo",)),
)
def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]: return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
