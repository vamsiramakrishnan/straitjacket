"""Small production policy seam evolved by the AlphaEvolve guard family.

This module stays stdlib-free beyond annotations because ``ctx.hook`` imports
it on every PreToolUse process startup.
"""

from __future__ import annotations

# EVOLVE-BLOCK-START


def choose_guard(state: dict, options: tuple = ()) -> str:
    """Choose the least-friction action that preserves containment and safety."""
    if state.get("secret") or state.get("outside_root"):
        return "force_ask"
    if state.get("explicit_deny") or state.get("destructive"):
        return "deny"
    if state.get("bounded_command") or state.get("structured_result"):
        return "allow"
    if state.get("large_read") or state.get("session_pressure"):
        return "rewrite_read"
    if state.get("flood_command") or state.get("readonly_noisy"):
        return "rewrite_command"
    if state.get("unknown_command"):
        return "force_ask"
    return "allow"


# EVOLVE-BLOCK-END
