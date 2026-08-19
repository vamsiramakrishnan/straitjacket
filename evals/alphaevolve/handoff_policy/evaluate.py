"""Evidence-gated evaluator for cross-harness checkpoint handoffs."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket checkpoint handoff budgeting"
METRIC_NAME = "evidence_adjusted_handoff_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
INTEGRATED_PROGRAM_CODE = (HERE.parents[2] / "src" / "ctx" / "handoff_policy.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_handoff"

OPTIONS = (
    option("address_only", ("address",), dollars=.001, visible_tokens=18, model_turns=0, tool_calls=0, latency_ms=1),
    option("compact", ("address","summary"), dollars=.003, visible_tokens=140, model_turns=0, tool_calls=0, latency_ms=2),
    option("standard", ("address","summary","decision"), dollars=.006, visible_tokens=300, model_turns=0, tool_calls=0, latency_ms=3),
    option("expanded", ("address","summary","decision","failure_detail"), dollars=.012, visible_tokens=700, model_turns=0, tool_calls=0, latency_ms=5),
)

SEARCH_CASES = (
    case("terminal_success", {"has_dependents":False}, ("address",), baseline="expanded", allowed=("address_only","compact","standard","expanded")),
    case("explore_handoff", {"has_dependents":True,"output_bytes":20000}, ("address","summary"), baseline="expanded", allowed=("compact","standard","expanded")),
    case("mutation_handoff", {"has_dependents":True,"mutation":True}, ("address","summary","decision"), baseline="expanded", allowed=("standard","expanded")),
    case("verification_handoff", {"has_dependents":True,"verification":True}, ("address","summary","decision"), baseline="expanded", allowed=("standard","expanded")),
    case("failed_node", {"failed":True,"has_dependents":True}, ("address","summary","decision","failure_detail"), baseline="expanded", allowed=("expanded",)),
)
HOLDOUT_CASES = (
    case("small_dependency", {"has_dependents":True,"output_bytes":100}, ("address","summary"), baseline="expanded", allowed=("compact","standard","expanded")),
    case("terminal_verifier", {"has_dependents":False,"verification":True}, ("address","summary","decision"), baseline="expanded", allowed=("standard","expanded")),
)
ADVERSARIAL_CASES = (
    case("failure_cannot_hide", {"failed":True,"output_bytes":1000000}, ("address","summary","decision","failure_detail"), baseline="expanded", allowed=("expanded",)),
    case("mutation_needs_decision", {"mutation":True,"output_bytes":1}, ("address","summary","decision"), baseline="expanded", allowed=("standard","expanded")),
)


def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
