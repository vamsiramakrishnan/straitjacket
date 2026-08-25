"""Production-shaped evidence-plan policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_plan(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("provided_context"):
        return "direct"
    if state.get("failure"):
        return "diagnose_join"
    if state.get("symbol"):
        return "refs_context"
    if state.get("changes"):
        return "diff_verify"
    return "bounded_investigate"

# EVOLVE-BLOCK-END
