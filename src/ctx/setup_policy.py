"""Production setup strategy selected by the AlphaEvolve DevEx experiment."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START


def choose_setup(
    state: Mapping[str, Any], options: Sequence[Mapping[str, Any]] = ()
) -> str:
    """Choose the least-friction setup action that preserves correctness."""
    if state.get("unmanaged_conflict"):
        return "refuse_unmanaged"
    if state.get("receipt_current") and not state.get("force_repair"):
        return "ready_noop"
    if state.get("had_receipt"):
        return "repair_managed"
    if state.get("explicit"):
        return "configure_explicit"
    if state.get("installed_hosts"):
        return "configure_detected"
    return "configure_all"


# EVOLVE-BLOCK-END
