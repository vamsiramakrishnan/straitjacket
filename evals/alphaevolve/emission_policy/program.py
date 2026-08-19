"""Production-shaped output-emission policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_emission(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("failure"):
        return "typed_failure"
    if state.get("small") and not state.get("derived_evidence"):
        return "raw_small"
    if state.get("starved"):
        return "dense_digest"
    return "standard_digest"

# EVOLVE-BLOCK-END
