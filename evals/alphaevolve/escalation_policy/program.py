"""Seed policy for retry, retrieval, replan, escalation, and honest stop."""

from __future__ import annotations

from typing import Any, Mapping


# EVOLVE-BLOCK-START


def choose_recovery(state: Mapping[str, Any]) -> str:
    actions = {str(action["id"]): action for action in state.get("actions", ())}
    failure = str(state.get("failure_kind", "unknown"))
    attempts = int(state.get("attempts", 1))
    budget = float(state.get("budget_remaining_usd", 0.0))

    preferences: list[str]
    if failure in {"permission_denied", "auth_failure", "safety_denied"}:
        preferences = ["stop_blocked"]
    elif failure in {"missing_evidence", "context_omission"}:
        preferences = ["focused_retrieve", "replan"]
    elif failure in {"incomplete_contract", "verification_failure"}:
        preferences = ["replan", "escalate"]
    elif failure in {"transient_transport", "rate_limited"} and attempts < 2:
        preferences = ["retry_same", "replan"]
    elif failure in {"capability_limit", "repeated_incomplete"}:
        preferences = ["escalate", "replan"]
    elif failure == "stalled":
        # Silent for idle_timeout: a stuck model, not a slow one. The same
        # model again is the one action with no evidence behind it.
        preferences = ["escalate", "replan", "stop_blocked"]
    elif failure == "wall_timeout":
        # Still working when the wall clock ran out: the node was too big.
        # The coordinator gets first say (it can split it); the same model
        # continues in the same worktree only when nobody can re-plan.
        preferences = ["replan", "retry_same", "escalate"]
    else:
        preferences = ["replan", "escalate", "stop_blocked"]

    for action_id in preferences:
        action = actions.get(action_id)
        if action is not None and float(action.get("cost_usd", 0.0)) <= budget:
            return action_id
    if "stop_budget" in actions:
        return "stop_budget"
    return min(actions) if actions else ""


# EVOLVE-BLOCK-END
