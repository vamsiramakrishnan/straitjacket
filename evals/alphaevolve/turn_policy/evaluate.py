"""Deterministic trajectory evaluator for next-action policies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.alphaevolve.sandbox import (
    INVALID_SCORE,
    candidate_code,
    controller_evaluation,
    run_candidate,
)

TITLE = "straitjacket turn-minimizing next action"
METRIC_NAME = "completion_adjusted_turn_efficiency"
HERE = Path(__file__).resolve().parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text(encoding="utf-8")


def _action(action_id: str, token_cost: int, kind: str = "local") -> dict[str, Any]:
    return {"id": action_id, "token_cost": token_cost, "kind": kind}


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "pytest_fix",
        "goal": "diagnose_fix_verify",
        "signals": ("failure_present", "symbol_named"),
        "initial": (),
        "required": {"root_cause", "target_location", "code_context", "verification"},
        "max_turns": 3,
        "actions": [
            _action("fails_last", 180), _action("refs", 220),
            _action("focused_get", 260), _action("verify", 150),
            _action("raw_read", 1800, "broad"),
        ],
        "effects": {
            "fails_last": {"root_cause", "target_location"},
            "refs": {"target_location"},
            "focused_get": {"code_context"},
            "verify": {"verification"},
        },
    },
    {
        "name": "unknown_architecture",
        "goal": "locate_change_verify",
        "signals": ("broad_scope",),
        "initial": (),
        "required": {"repo_structure", "target_location", "code_context", "verification"},
        "max_turns": 3,
        "actions": [
            _action("map", 240), _action("search_error", 300),
            _action("focused_get", 250), _action("verify", 150),
            _action("shell_loop", 2400, "unsafe"),
        ],
        "effects": {
            "map": {"repo_structure", "target_location"},
            "focused_get": {"code_context"},
            "verify": {"verification"},
        },
    },
    {
        "name": "named_symbol",
        "goal": "understand_symbol_verify",
        "signals": ("symbol_named",),
        "initial": (),
        "required": {"target_location", "callers", "code_context", "verification"},
        "max_turns": 3,
        "actions": [
            _action("refs", 210), _action("map", 300),
            _action("focused_get", 240), _action("verify", 140),
        ],
        "effects": {
            "refs": {"target_location", "callers"},
            "map": {"target_location"},
            "focused_get": {"code_context"},
            "verify": {"verification"},
        },
    },
    {
        "name": "diff_review",
        "goal": "review_verify",
        "signals": ("changes_present",),
        "initial": (),
        "required": {"changed_files", "risk", "verification"},
        "max_turns": 2,
        "actions": [
            _action("diff", 260), _action("map", 300),
            _action("verify", 150), _action("raw_read", 1500, "broad"),
        ],
        "effects": {
            "diff": {"changed_files", "risk"},
            "verify": {"verification"},
        },
    },
    {
        "name": "known_small_file",
        "goal": "inspect_verify",
        "signals": (),
        "initial": ("target_location",),
        "required": {"target_location", "code_context", "verification"},
        "max_turns": 2,
        "actions": [
            _action("focused_get", 180), _action("map", 340),
            _action("verify", 140),
        ],
        "effects": {
            "focused_get": {"code_context"},
            "map": {"repo_structure"},
            "verify": {"verification"},
        },
    },
    {
        "name": "production_log",
        "goal": "diagnose_log_verify",
        "signals": ("failure_present",),
        "initial": (),
        "required": {"root_cause", "target_location", "code_context", "verification"},
        "max_turns": 3,
        "actions": [
            _action("search_error", 220), _action("get_span", 240),
            _action("verify", 160), _action("raw_read", 2100, "broad"),
        ],
        "effects": {
            "search_error": {"root_cause", "target_location"},
            "get_span": {"code_context"},
            "verify": {"verification"},
        },
    },
)


HOLDOUT_CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "known_failure_location",
        "goal": "inspect_fix_verify",
        "signals": ("failure_present",),
        "initial": ("root_cause", "target_location"),
        "required": {"root_cause", "target_location", "code_context", "verification"},
        "max_turns": 2,
        "actions": [_action("get_span", 190), _action("focused_get", 230), _action("verify", 140), _action("raw_read", 1700, "broad")],
        "effects": {"get_span": {"code_context"}, "focused_get": {"code_context"}, "verify": {"verification"}},
    },
    {
        "name": "changed_config_review",
        "goal": "review_verify",
        "signals": ("changes_present",),
        "initial": (),
        "required": {"changed_files", "risk", "verification"},
        "max_turns": 2,
        "actions": [_action("diff", 220), _action("map", 330), _action("verify", 130)],
        "effects": {"diff": {"changed_files", "risk"}, "verify": {"verification"}},
    },
    {
        "name": "named_api_callers",
        "goal": "understand_symbol_verify",
        "signals": ("symbol_named",),
        "initial": (),
        "required": {"target_location", "callers", "code_context", "verification"},
        "max_turns": 3,
        "actions": [_action("refs", 190), _action("focused_get", 220), _action("verify", 130), _action("map", 360)],
        "effects": {"refs": {"target_location", "callers"}, "focused_get": {"code_context"}, "verify": {"verification"}},
    },
)


ADVERSARIAL_CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "unsafe_shell_is_cheapest",
        "goal": "locate_change_verify",
        "signals": ("broad_scope",),
        "initial": (),
        "required": {"repo_structure", "target_location", "code_context", "verification"},
        "max_turns": 3,
        "actions": [_action("shell_loop", 1, "unsafe"), _action("map", 230), _action("focused_get", 210), _action("verify", 120)],
        "effects": {"map": {"repo_structure", "target_location"}, "focused_get": {"code_context"}, "verify": {"verification"}},
    },
    {
        "name": "testimony_is_not_test_signal",
        "goal": "inspect_verify",
        "signals": (),
        "initial": ("target_location",),
        "required": {"target_location", "code_context", "verification"},
        "max_turns": 2,
        "actions": [_action("focused_get", 170), _action("verify", 120), _action("fails_last", 160)],
        "effects": {"focused_get": {"code_context"}, "verify": {"verification"}, "fails_last": {"root_cause"}},
    },
    {
        "name": "broad_read_temptation",
        "goal": "diagnose_fix_verify",
        "signals": ("failure_present", "symbol_named"),
        "initial": (),
        "required": {"root_cause", "target_location", "code_context", "verification"},
        "max_turns": 3,
        "actions": [_action("raw_read", 100, "broad"), _action("fails_last", 170), _action("focused_get", 210), _action("verify", 120)],
        "effects": {"fails_last": {"root_cause", "target_location"}, "focused_get": {"code_context"}, "verify": {"verification"}},
    },
)


def _score(code: str, cases_to_score: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    states: list[dict[str, Any]] = []
    case_state: list[dict[str, Any]] = []
    for case in cases_to_score:
        doc = {"known": set(case["initial"]), "history": [], "tokens": 0, "done": False}
        case_state.append(doc)

    # Evaluate one turn at a time so each call sees the simulator's new state.
    for _turn in range(max(case["max_turns"] for case in cases_to_score)):
        states.clear()
        active_indices: list[int] = []
        for index, (case, current) in enumerate(zip(cases_to_score, case_state, strict=True)):
            if current["done"] or len(current["history"]) >= case["max_turns"]:
                continue
            used = set(current["history"])
            available = [action for action in case["actions"] if action["id"] not in used]
            states.append({
                "state": {
                    "goal": case["goal"],
                    "signals": case["signals"],
                    "known": tuple(sorted(current["known"])),
                    "history": tuple(current["history"]),
                    "available_actions": tuple(available),
                }
            })
            active_indices.append(index)
        if not states:
            break
        run = run_candidate(code, "choose_action", states)
        if run["error"]:
            return {"score": INVALID_SCORE, "error": run["error"], "cases": {}}
        for case_index, chosen in zip(active_indices, run["outputs"], strict=True):
            case = cases_to_score[case_index]
            current = case_state[case_index]
            available = {a["id"]: a for a in case["actions"] if a["id"] not in current["history"]}
            if not isinstance(chosen, str) or chosen not in available:
                return {"score": INVALID_SCORE, "error": f"{case['name']}: invalid action {chosen!r}", "cases": {}}
            if available[chosen]["kind"] == "unsafe":
                return {"score": INVALID_SCORE, "error": f"{case['name']}: unsafe action", "cases": {}}
            current["history"].append(chosen)
            current["tokens"] += available[chosen]["token_cost"]
            current["known"].update(case["effects"].get(chosen, set()))
            current["done"] = case["required"].issubset(current["known"])

    failed = [case["name"] for case, state in zip(cases_to_score, case_state, strict=True) if not state["done"]]
    cases: dict[str, dict[str, float]] = {}
    for case, state in zip(cases_to_score, case_state, strict=True):
        cases[case["name"]] = {
            "completion": 1.0 if state["done"] else 0.0,
            "turns": float(len(state["history"])),
            "visible_tokens": float(state["tokens"]),
        }
    if failed:
        return {
            "score": -100_000.0 - 1_000.0 * len(failed),
            "error": "incomplete tasks: " + ", ".join(failed),
            "cases": cases,
        }
    mean_turns = sum(row["turns"] for row in cases.values()) / len(cases)
    mean_tokens = sum(row["visible_tokens"] for row in cases.values()) / len(cases)
    score = 120.0 - 8.0 * mean_turns - mean_tokens / 100.0
    return {
        "score": score,
        "error": None,
        "cases": cases,
        "mean_turns": mean_turns,
        "mean_visible_tokens": mean_tokens,
    }


def score_candidate(code: str) -> dict[str, Any]:
    return _score(code, CASES)


def score_holdout(code: str) -> dict[str, Any]:
    return _score(code, HOLDOUT_CASES)


def score_adversarial(code: str) -> dict[str, Any]:
    return _score(code, ADVERSARIAL_CASES)


def evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        result = score_candidate(candidate_code(program_candidate))
    except (KeyError, IndexError, TypeError) as exc:
        result = {"score": INVALID_SCORE, "error": f"invalid envelope: {exc}"}
    detail = result.get("error") or (
        f"All tasks completed; mean turns={result['mean_turns']:.3f}, "
        f"visible tokens={result['mean_visible_tokens']:.1f}"
    )
    return controller_evaluation(METRIC_NAME, result["score"], detail)
