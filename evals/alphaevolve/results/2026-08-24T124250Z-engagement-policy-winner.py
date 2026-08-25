"""Production-shaped engagement/reflex policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_engagement(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("starved"):
        return "dense"
    if state.get("repeated_positive"):
        return "bypass"
    if state.get("truncated") or state.get("window_hot") or int(state.get("calls", 0)) >= 8:
        return "active_lean" if state.get("lean_model") else "active_full"
    return "passive"

# EVOLVE-BLOCK-END
