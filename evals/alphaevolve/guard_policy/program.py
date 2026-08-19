"""Production-shaped birth-gate policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_guard(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("secret") or state.get("outside_root"):
        return "force_ask"
    if state.get("explicit_deny") or state.get("destructive"):
        return "deny"
    if state.get("large_read") or state.get("session_pressure"):
        return "rewrite_read"
    if state.get("flood_command"):
        return "rewrite_command"
    return "allow"

# EVOLVE-BLOCK-END
