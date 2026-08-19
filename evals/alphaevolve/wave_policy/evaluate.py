"""Completion- and mutation-gated evaluator for orchestration waves."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket safe orchestration wave scheduling"
METRIC_NAME = "completion_adjusted_wave_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
INTEGRATED_PROGRAM_CODE = (HERE.parents[2] / "src" / "ctx" / "wave_policy.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_wave"

OPTIONS = (
    option("serial", ("complete", "workspace_safe", "rate_safe", "mutation_isolated"), dollars=.12, visible_tokens=900, model_turns=4, tool_calls=4, latency_ms=400),
    option("parallel_two", ("complete", "workspace_safe", "parallel"), dollars=.12, visible_tokens=760, model_turns=3, tool_calls=4, latency_ms=240),
    option("parallel_four", ("complete", "workspace_safe", "parallel"), dollars=.12, visible_tokens=680, model_turns=2, tool_calls=4, latency_ms=140),
    option("readonly_first", ("complete", "workspace_safe", "parallel", "mutation_isolated"), dollars=.12, visible_tokens=720, model_turns=3, tool_calls=4, latency_ms=230),
    option("mutation_serial", ("complete", "workspace_safe", "mutation_isolated"), dollars=.12, visible_tokens=840, model_turns=4, tool_calls=4, latency_ms=360),
)

SEARCH_CASES = (
    case("single_read", {"ready_count":1,"readonly_count":1}, ("complete","workspace_safe"), baseline="serial", allowed=("serial",)),
    case("two_reads", {"ready_count":2,"readonly_count":2}, ("complete","workspace_safe"), baseline="serial", allowed=("serial","parallel_two")),
    case("wide_reads", {"ready_count":7,"readonly_count":7}, ("complete","workspace_safe"), baseline="serial", allowed=("serial","parallel_two","parallel_four")),
    case("mixed_wave", {"ready_count":4,"readonly_count":3,"mutation_count":1}, ("complete","workspace_safe","mutation_isolated"), baseline="serial", allowed=("readonly_first","mutation_serial")),
    case("two_mutations", {"ready_count":2,"mutation_count":2}, ("complete","workspace_safe","mutation_isolated"), baseline="serial", allowed=("mutation_serial",)),
)
HOLDOUT_CASES = (
    case("three_reads", {"ready_count":3,"readonly_count":3}, ("complete","workspace_safe"), baseline="serial", allowed=("serial","parallel_two","parallel_four")),
    case("mutation_plus_read", {"ready_count":2,"readonly_count":1,"mutation_count":1}, ("complete","workspace_safe","mutation_isolated"), baseline="serial", allowed=("readonly_first","mutation_serial")),
)
ADVERSARIAL_CASES = (
    case("rate_limited_reads", {"ready_count":8,"readonly_count":8,"provider_rate_limited":True}, ("complete","workspace_safe","rate_safe"), baseline="serial", allowed=("serial",)),
    case("rate_limited_mutation", {"ready_count":3,"readonly_count":2,"mutation_count":1,"provider_rate_limited":True}, ("complete","workspace_safe","mutation_isolated"), baseline="serial", allowed=("mutation_serial",)),
)


def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)


def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
