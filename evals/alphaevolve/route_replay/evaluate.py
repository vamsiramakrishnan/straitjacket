"""Completion-gated evaluator informed by explicit route-run labels."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterable

from evals.alphaevolve.sandbox import (
    INVALID_SCORE,
    candidate_code,
    controller_evaluation,
    run_candidate,
)

TITLE = "straitjacket receipt-informed route compiler"
METRIC_NAME = "verified_route_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")
OBSERVATIONS = json.loads((HERE / "observations.json").read_text(encoding="utf-8"))[
    "observations"
]


def _route(
    route_id: str,
    capabilities: Iterable[str],
    *,
    unattended: bool,
    verification: bool,
    visible_tokens: int,
    model_turns: int,
    tool_calls: int,
    dollars: float,
    latency_ms: float,
    signature: Iterable[tuple[str, str, str]] = (),
) -> dict[str, Any]:
    return {
        "id": route_id,
        "capabilities": tuple(capabilities),
        "unattended": unattended,
        "verification": verification,
        "visible_tokens": visible_tokens,
        "model_turns": model_turns,
        "tool_calls": tool_calls,
        "dollars": dollars,
        "latency_ms": latency_ms,
        "signature": tuple(tuple(item) for item in signature),
    }


ROUTES: tuple[dict[str, Any], ...] = (
    _route("cheap_noop", (), unattended=True, verification=False, visible_tokens=200, model_turns=1, tool_calls=0, dollars=0.005, latency_ms=500),
    _route("focused_answer", ("read", "answer"), unattended=True, verification=False, visible_tokens=15000, model_turns=1, tool_calls=1, dollars=0.075, latency_ms=43330, signature=(("answer", "codex", "gpt-5.6-terra"),)),
    _route("focused_review", ("diff", "answer"), unattended=True, verification=False, visible_tokens=22500, model_turns=1, tool_calls=1, dollars=0.0975, latency_ms=89391, signature=(("review", "claude", "claude-sonnet-4.6"),)),
    # The first live route used the vendor agy CLI. It failed its explicit
    # named-test label and is interactive, so it remains present as a trap: an
    # optimizer may not select it merely because its estimate is cheaper.
    _route("failed_interactive_test", ("test",), unattended=False, verification=True, visible_tokens=22500, model_turns=2, tool_calls=1, dollars=0.04375, latency_ms=5002, signature=(("verify", "antigravity", "gemini-3.5-flash-lite"),)),
    # Receipt route-18ccc49db7d6057c: named test passed on Claude/Haiku.
    _route("proven_unattended_test", ("test",), unattended=True, verification=True, visible_tokens=22500, model_turns=1, tool_calls=1, dollars=0.0325, latency_ms=10678, signature=(("verify", "claude", "claude-haiku-4.5"),)),
    _route("focused_edit_verify", ("read", "edit", "verify"), unattended=True, verification=True, visible_tokens=51000, model_turns=2, tool_calls=3, dollars=0.160, latency_ms=39601, signature=(("implement", "codex", "gpt-5.6-terra"), ("verify", "claude", "claude-haiku-4.5"))),
    _route("failed_lean_codex", ("search", "read", "edit", "verify", "test"), unattended=True, verification=True, visible_tokens=70000, model_turns=3, tool_calls=5, dollars=0.218, latency_ms=80307, signature=(("explore", "claude", "claude-haiku-4.5"), ("implement", "codex", "gpt-5.6-terra"), ("verify", "claude", "claude-haiku-4.5"))),
    _route("lean_explicit_feature", ("search", "read", "edit", "verify", "test"), unattended=True, verification=True, visible_tokens=70000, model_turns=3, tool_calls=5, dollars=0.234, latency_ms=65689, signature=(("explore", "claude", "claude-haiku-4.5"), ("implement", "claude", "claude-sonnet-4.6"), ("verify", "claude", "claude-haiku-4.5"))),
    _route("complete_general", ("search", "read", "diff", "answer", "edit", "verify", "test", "plan"), unattended=True, verification=True, visible_tokens=125500, model_turns=4, tool_calls=7, dollars=0.7915, latency_ms=202687, signature=(("explore", "claude", "claude-haiku-4.5"), ("plan", "claude", "claude-opus-4.8"), ("implement", "codex", "gpt-5.6-terra"), ("verify", "claude", "claude-haiku-4.5"))),
)


def _case(
    name: str,
    required: Iterable[str],
    *,
    source: str = "contract",
    known_failed: Iterable[str] = (),
    requires_live_evidence: bool = False,
    **profile: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "required": frozenset(required),
        "source": source,
        "known_failed": frozenset(known_failed),
        "requires_live_evidence": requires_live_evidence,
        "profile": profile,
    }


SEARCH_CASES: tuple[dict[str, Any], ...] = (
    _case("live_named_test", ("test",), source="live_named_test", known_failed=("failed_interactive_test",), requires_live_evidence=True, kind="test", high_confidence=True, mutation=False, review=False, verification_required=False, characters=107, words=6, multiline=False),
    _case("live_explain_symbol", ("read", "answer"), source="live_reviewed_output", requires_live_evidence=True, kind="answer", high_confidence=True, mutation=False, review=False, verification_required=False, characters=75, words=8, multiline=False),
    _case("live_inspect_file", ("read", "answer"), source="live_reviewed_output", requires_live_evidence=True, kind="inspect", high_confidence=True, mutation=False, review=False, verification_required=False, characters=99, words=12, multiline=False),
    _case("live_review_diff", ("diff", "answer"), source="live_reviewed_output", requires_live_evidence=True, kind="review", high_confidence=True, mutation=False, review=True, verification_required=False, characters=46, words=7, multiline=False),
    _case("live_small_edit", ("read", "edit", "verify"), source="live_acceptance", requires_live_evidence=True, kind="simple_edit", high_confidence=True, mutation=True, review=False, verification_required=True, characters=39, words=9, multiline=False),
    _case("live_explicit_feature", ("search", "read", "edit", "verify", "test"), source="live_acceptance", kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=229, words=31, multiline=False, named_target=True, named_acceptance=True, high_risk_scope=False, explicit_contract=True),
    _case("underspecified_feature", ("search", "read", "edit", "verify", "plan"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=126, words=17, multiline=False, named_target=True, named_acceptance=True, high_risk_scope=False, explicit_contract=False),
    _case("ambiguous_feature", ("search", "read", "edit", "verify", "plan"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=82, words=12, multiline=False, named_target=False, named_acceptance=False, high_risk_scope=False, explicit_contract=False),
)

HOLDOUT_CASES: tuple[dict[str, Any], ...] = (
    _case("another_named_test", ("test",), known_failed=("failed_interactive_test",), kind="test", high_confidence=True, mutation=False, review=False, verification_required=False, characters=70, words=9, multiline=False),
    _case("small_doc_edit", ("read", "edit", "verify"), kind="simple_edit", high_confidence=True, mutation=True, review=False, verification_required=True, characters=51, words=8, multiline=False),
    _case("bounded_explicit_feature", ("search", "read", "edit", "verify", "test"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=174, words=24, multiline=False, named_target=True, named_acceptance=True, high_risk_scope=False, explicit_contract=True),
    _case("unknown_migration", ("search", "read", "edit", "verify", "plan"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=103, words=14, multiline=False, named_target=True, named_acceptance=True, high_risk_scope=True, explicit_contract=True),
)

ADVERSARIAL_CASES: tuple[dict[str, Any], ...] = (
    _case("latest_release", ("search", "read", "edit", "verify", "plan"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=63, words=9, multiline=False, named_target=False, named_acceptance=False, high_risk_scope=False, explicit_contract=False),
    _case("customer_testimony", ("search", "read", "edit", "verify", "plan"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=59, words=8, multiline=False, named_target=False, named_acceptance=False, high_risk_scope=False, explicit_contract=False),
    _case("explicit_security_change", ("search", "read", "edit", "verify", "plan"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=180, words=25, multiline=False, named_target=True, named_acceptance=True, high_risk_scope=True, explicit_contract=True),
    _case("multiline_unknown", ("search", "read", "edit", "verify", "plan"), kind="general", high_confidence=False, mutation=True, review=False, verification_required=True, characters=220, words=32, multiline=True, named_target=True, named_acceptance=True, high_risk_scope=False, explicit_contract=False),
)


_PROFILE_MATCH_FIELDS = (
    "named_target",
    "named_acceptance",
    "high_risk_scope",
    "explicit_contract",
)


def _matching_observations(
    case: dict[str, Any], route: dict[str, Any]
) -> list[dict[str, Any]]:
    expected_signature = tuple(tuple(item) for item in route.get("signature", ()))
    return [
        observation
        for observation in OBSERVATIONS
        if observation.get("profile", {}).get("kind") == case["profile"].get("kind")
        and tuple(
            (node.get("role"), node.get("host"), node.get("model"))
            for node in observation.get("route", ())
        )
        == expected_signature
        and all(
            case["profile"].get(field) == observation.get("profile", {}).get(field)
            for field in _PROFILE_MATCH_FIELDS
            if field in case["profile"]
        )
    ]


def _observed_actual_cost(
    case: dict[str, Any], route: dict[str, Any]
) -> float | None:
    """Median complete provider/token-derived cost for a matching successful run."""
    costs: list[float] = []
    for observation in _matching_observations(case, route):
        usage = observation.get("measurement", {}).get("actual_usage")
        if (
            observation.get("label", {}).get("task_success") is True
            and observation.get("measurement", {}).get("route_completed") is True
            and isinstance(usage, dict)
            and usage.get("status") == "available"
            and usage.get("cost_complete") is True
            and isinstance(usage.get("cost_usd"), (int, float))
            and not isinstance(usage.get("cost_usd"), bool)
        ):
            costs.append(float(usage["cost_usd"]))
    return statistics.median(costs) if costs else None


def _totals(
    route_ids: list[str],
    routes: dict[str, dict[str, Any]],
    cases: tuple[dict[str, Any], ...],
) -> tuple[dict[str, float], dict[str, int]]:
    totals = {
        field: sum(float(routes[route_id][field]) for route_id in route_ids)
        for field in ("visible_tokens", "model_turns", "tool_calls", "latency_ms")
    }
    dollars = 0.0
    actual_cases = 0
    for case, route_id in zip(cases, route_ids, strict=True):
        route = routes[route_id]
        actual = _observed_actual_cost(case, route)
        if actual is not None:
            dollars += actual
            actual_cases += 1
        else:
            dollars += float(route["dollars"])
    totals["dollars"] = dollars
    return totals, {
        "actual": actual_cases,
        "estimated": len(route_ids) - actual_cases,
    }


def _live_success(case: dict[str, Any], route: dict[str, Any]) -> bool:
    """Require an explicit matching semantic label, never an exit-code proxy."""
    return any(
        observation.get("label", {}).get("task_success") is True
        for observation in _matching_observations(case, route)
    )


def _score_cases(code: str, cases: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    run = run_candidate(
        code,
        "choose_route",
        [{"profile": case["profile"], "routes": ROUTES} for case in cases],
    )
    if run["error"]:
        return {"score": INVALID_SCORE, "error": run["error"], "cases": {}}

    routes = {route["id"]: route for route in ROUTES}
    selected: list[str] = []
    failed: list[str] = []
    details: dict[str, dict[str, Any]] = {}
    for case, route_id in zip(cases, run["outputs"], strict=True):
        if not isinstance(route_id, str) or route_id not in routes:
            return {"score": INVALID_SCORE, "error": f"{case['name']}: invalid route {route_id!r}", "cases": {}}
        route = routes[route_id]
        completion = case["required"].issubset(set(route["capabilities"]))
        unattended = bool(route["unattended"])
        verified = not case["profile"].get("mutation") or bool(route["verification"])
        profile_admissible = route_id not in {
            "lean_explicit_feature",
            "failed_lean_codex",
        } or (
            bool(case["profile"].get("named_target"))
            and bool(case["profile"].get("named_acceptance"))
            and bool(case["profile"].get("explicit_contract"))
            and not bool(case["profile"].get("high_risk_scope"))
        )
        route_requires_live = route_id in {
            "lean_explicit_feature",
            "failed_lean_codex",
        }
        empirical = route_id not in case["known_failed"] and (
            not (case["requires_live_evidence"] or route_requires_live)
            or _live_success(case, route)
        )
        passed = completion and unattended and verified and profile_admissible and empirical
        if not passed:
            failed.append(case["name"])
        selected.append(route_id)
        actual_cost = _observed_actual_cost(case, route)
        details[case["name"]] = {
            "route": route_id,
            "completion": 1.0 if passed else 0.0,
            "source": case["source"],
            "unattended": unattended,
            "verification": verified,
            "empirical": empirical,
            "profile_admissible": profile_admissible,
            "dollars": actual_cost if actual_cost is not None else float(route["dollars"]),
            "dollar_source": "actual_usage" if actual_cost is not None else "estimate",
        }

    if failed:
        return {
            "score": -100_000.0 - 1_000.0 * len(failed),
            "error": "inadmissible or incomplete routes: " + ", ".join(failed),
            "cases": details,
        }

    totals, cost_coverage = _totals(selected, routes, cases)
    baseline, baseline_cost_coverage = _totals(
        ["complete_general"] * len(cases), routes, cases
    )
    reductions = {field: 1.0 - totals[field] / baseline[field] for field in totals}
    score = (
        100.0
        + 30.0 * reductions["dollars"]
        + 25.0 * reductions["visible_tokens"]
        + 20.0 * reductions["model_turns"]
        + 10.0 * reductions["tool_calls"]
        + 15.0 * reductions["latency_ms"]
    )
    return {
        "score": score,
        "error": None,
        "cases": details,
        "totals": totals,
        "baseline": baseline,
        "reductions": reductions,
        "cost_coverage": cost_coverage,
        "baseline_cost_coverage": baseline_cost_coverage,
    }


def score_candidate(code: str) -> dict[str, Any]:
    return _score_cases(code, SEARCH_CASES)


def score_holdout(code: str) -> dict[str, Any]:
    return _score_cases(code, HOLDOUT_CASES)


def score_adversarial(code: str) -> dict[str, Any]:
    return _score_cases(code, ADVERSARIAL_CASES)


def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        result = score_candidate(candidate_code(program_candidate))
    except (KeyError, IndexError, TypeError) as exc:
        result = {"score": INVALID_SCORE, "error": f"invalid envelope: {exc}"}
    detail = result.get("error") or (
        f"All routes admissible; score={result['score']:.3f}, "
        f"dollars={result['totals']['dollars']:.4f}, "
        f"visible_tokens={result['totals']['visible_tokens']:.0f}"
    )
    return controller_evaluation(METRIC_NAME, result["score"], detail)
