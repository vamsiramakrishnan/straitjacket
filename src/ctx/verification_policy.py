"""Reviewed AlphaEvolve policy for orchestration verification routing."""

from __future__ import annotations

# EVOLVE-BLOCK-START


def choose_verification(state: dict, options: tuple = ()) -> str:
    """Buy independence only when task risk or mutation complexity warrants it."""
    alternate = bool(state.get("alternate_host"))
    if state.get("high_risk"):
        return "independent_standard" if alternate else "focused_standard"
    if state.get("mutation") and int(state.get("complexity", 1)) >= 3 and alternate:
        return "independent_economy"
    return "focused_economy"


# EVOLVE-BLOCK-END
