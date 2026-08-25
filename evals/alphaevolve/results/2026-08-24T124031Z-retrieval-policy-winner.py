"""Production-shaped retrieval policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_retrieval(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("address"):
        return "exact_span"
    if state.get("failure"):
        return "fails_then_span"
    if state.get("symbol"):
        return "refs_then_span"
    if state.get("unknown_scope"):
        return "map_then_get"
    return "focused_search"

# EVOLVE-BLOCK-END
