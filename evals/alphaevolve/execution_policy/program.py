"""Production-shaped execution/cache policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_execution(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("cache_valid"):
        return "cache_reuse"
    if state.get("cache_present") and not state.get("cache_valid"):
        return "rebuild"
    if state.get("long_running"):
        return "background_job"
    if state.get("flood"):
        return "foreground_captured"
    return "foreground_inline"

# EVOLVE-BLOCK-END
