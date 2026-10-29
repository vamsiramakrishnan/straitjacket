"""Production-shaped capability-surface policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_surface(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("high_risk") or state.get("unknown"):
        return "full"
    if state.get("provided_context"):
        return "minimal_answer"
    if state.get("phase") == "review":
        return "review"
    if state.get("mutation"):
        return "local_dev"
    return "read_only"

# EVOLVE-BLOCK-END
