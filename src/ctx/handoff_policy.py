"""Reviewed AlphaEvolve policy for bounded cross-harness handoffs."""

from __future__ import annotations

# EVOLVE-BLOCK-START


def choose_handoff(state: dict, options: tuple = ()) -> str:
    """Keep an address always and spend inline context only where it helps."""
    if state.get("failed"):
        return "expanded"
    if state.get("mutation") or state.get("verification"):
        return "standard"
    if not state.get("has_dependents"):
        return "address_only"
    return "compact"


# EVOLVE-BLOCK-END
