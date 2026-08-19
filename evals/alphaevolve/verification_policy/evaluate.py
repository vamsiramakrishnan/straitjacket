"""Completion- and independence-gated evaluator for verifier routing."""

from pathlib import Path
from typing import Any

from evals.alphaevolve.choice_eval import case, controller_adapter, option, score_choice_policy

TITLE = "straitjacket verification route selection"
METRIC_NAME = "risk_adjusted_verification_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
INTEGRATED_PROGRAM_CODE = (HERE.parents[2] / "src" / "ctx" / "verification_policy.py").read_text(encoding="utf-8")
FUNCTION_NAME = "choose_verification"

OPTIONS = (
    option("focused_economy", ("verified",), dollars=.018, visible_tokens=500, model_turns=1, tool_calls=1, latency_ms=120),
    option("focused_standard", ("verified","review"), dollars=.055, visible_tokens=900, model_turns=1, tool_calls=1, latency_ms=210),
    option("independent_economy", ("verified","independent"), dollars=.035, visible_tokens=720, model_turns=1, tool_calls=1, latency_ms=170),
    option("independent_standard", ("verified","independent","review"), dollars=.090, visible_tokens=1200, model_turns=1, tool_calls=1, latency_ms=290),
    option("frontier_review", ("verified","independent","review","architecture"), dollars=.280, visible_tokens=2400, model_turns=1, tool_calls=1, latency_ms=520),
)

SEARCH_CASES = (
    case("read_only", {"mutation":False,"complexity":1,"alternate_host":True}, ("verified",), baseline="independent_standard", allowed=("focused_economy","independent_economy","focused_standard","independent_standard")),
    case("simple_edit", {"mutation":True,"complexity":1,"alternate_host":True}, ("verified",), baseline="independent_standard", allowed=("focused_economy","independent_economy","focused_standard","independent_standard")),
    case("complex_edit", {"mutation":True,"complexity":4,"alternate_host":True}, ("verified","independent"), baseline="independent_standard", allowed=("independent_economy","independent_standard","frontier_review")),
    case("security_change", {"mutation":True,"complexity":4,"high_risk":True,"alternate_host":True}, ("verified","independent","review"), baseline="independent_standard", allowed=("independent_standard","frontier_review")),
    case("single_host_security", {"mutation":True,"complexity":4,"high_risk":True,"alternate_host":False}, ("verified","review"), baseline="focused_standard", allowed=("focused_standard",)),
)
HOLDOUT_CASES = (
    case("moderate_feature", {"mutation":True,"complexity":3,"alternate_host":True}, ("verified","independent"), baseline="independent_standard", allowed=("independent_economy","independent_standard")),
    case("single_host_small", {"mutation":True,"complexity":1,"alternate_host":False}, ("verified",), baseline="focused_standard", allowed=("focused_economy","focused_standard")),
)
ADVERSARIAL_CASES = (
    case("auth_boundary", {"mutation":True,"complexity":5,"high_risk":True,"alternate_host":True}, ("verified","independent","review"), baseline="independent_standard", allowed=("independent_standard","frontier_review")),
    case("high_risk_no_alternate", {"mutation":True,"complexity":5,"high_risk":True,"alternate_host":False}, ("verified","review"), baseline="focused_standard", allowed=("focused_standard",)),
)


def _score(code: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]: return score_choice_policy(code, FUNCTION_NAME, rows, OPTIONS)
def score_candidate(code: str) -> dict[str, Any]: return _score(code, SEARCH_CASES)
def score_holdout(code: str) -> dict[str, Any]: return _score(code, HOLDOUT_CASES)
def score_adversarial(code: str) -> dict[str, Any]: return _score(code, ADVERSARIAL_CASES)
def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    return controller_adapter(program_candidate, metric_name=METRIC_NAME, function_name=FUNCTION_NAME, cases=SEARCH_CASES, options=OPTIONS)
