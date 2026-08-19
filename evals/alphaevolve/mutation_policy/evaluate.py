"""Hard-gated evaluator for mutation isolation."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket mutation isolation"
METRIC_NAME = "safety_adjusted_mutation_parallelism"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
INTEGRATED_PROGRAM_CODE = (HERE.parents[2] / "src" / "ctx" / "mutation_policy.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_mutation_isolation"

OPTIONS = (
    option("readonly_shared", ("complete","isolated","parallel"), dollars=.02, visible_tokens=120, model_turns=1, tool_calls=2, latency_ms=80),
    option("serial_workspace", ("complete","isolated","merge_safe"), dollars=.08, visible_tokens=340, model_turns=3, tool_calls=3, latency_ms=360),
    option("parallel_worktrees", ("complete","isolated","merge_safe","parallel"), dollars=.07, visible_tokens=260, model_turns=2, tool_calls=5, latency_ms=190),
    option("parallel_shared", ("complete","parallel"), dollars=.04, visible_tokens=180, model_turns=2, tool_calls=3, latency_ms=130, safe=False),
)

SEARCH_CASES = (
    case("readonly", {"mutation_count":0}, ("complete","isolated"), baseline="serial_workspace", allowed=("readonly_shared","serial_workspace")),
    case("one_mutation", {"mutation_count":1,"shared_workspace":True}, ("complete","isolated","merge_safe"), baseline="serial_workspace", allowed=("serial_workspace",)),
    case("shared_two", {"mutation_count":2,"shared_workspace":True}, ("complete","isolated","merge_safe"), baseline="serial_workspace", allowed=("serial_workspace",)),
    case("disjoint_worktrees", {"mutation_count":3,"isolated_worktrees":True,"targets_declared":True,"target_overlap":False}, ("complete","isolated","merge_safe"), baseline="serial_workspace", allowed=("serial_workspace","parallel_worktrees")),
)
HOLDOUT_CASES = (
    case("disjoint_pair", {"mutation_count":2,"isolated_worktrees":True,"targets_declared":True,"target_overlap":False}, ("complete","isolated","merge_safe"), baseline="serial_workspace", allowed=("serial_workspace","parallel_worktrees")),
    case("undeclared_targets", {"mutation_count":2,"isolated_worktrees":True,"targets_declared":False}, ("complete","isolated","merge_safe"), baseline="serial_workspace", allowed=("serial_workspace",)),
)
ADVERSARIAL_CASES = (
    case("overlapping_worktrees", {"mutation_count":2,"isolated_worktrees":True,"targets_declared":True,"target_overlap":True}, ("complete","isolated","merge_safe"), baseline="serial_workspace", allowed=("serial_workspace",)),
    case("claimed_disjoint_shared", {"mutation_count":2,"shared_workspace":True,"targets_declared":True,"target_overlap":False}, ("complete","isolated","merge_safe"), baseline="serial_workspace", allowed=("serial_workspace",)),
)


def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
