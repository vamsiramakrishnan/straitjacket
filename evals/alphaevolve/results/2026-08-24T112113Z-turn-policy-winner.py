"""Seed policy for choosing one bounded next action."""

from __future__ import annotations

from typing import Any, Mapping


# EVOLVE-BLOCK-START


def choose_action(state: Mapping[str, Any]) -> str:
    """Choose one action ID from state['available_actions']."""
    available = {str(action["id"]): action for action in state.get("available_actions", ())}
    known = set(state.get("known", ()))
    signals = set(state.get("signals", ()))

    preferences: list[str] = []
    if "failure_present" in signals and "root_cause" not in known:
        preferences += ["fails_last", "search_error"]
    if "broad_scope" in signals and "target_location" not in known:
        preferences += ["map"]
    if "symbol_named" in signals and "target_location" not in known:
        preferences += ["refs"]
    if "changes_present" in signals and "changed_files" not in known:
        preferences += ["diff"]
    if "target_location" in known and "code_context" not in known:
        preferences += ["focused_get"]
    if "root_cause" in known and "code_context" not in known:
        preferences += ["get_span", "focused_get"]
    if "code_context" in known and "verification" not in known:
        preferences += ["verify"]

    for action_id in preferences:
        if action_id in available:
            return action_id
    if not available:
        return ""
    return min(available.values(), key=lambda action: (int(action.get("token_cost", 0)), str(action["id"]))) ["id"]

# EVOLVE-BLOCK-END
