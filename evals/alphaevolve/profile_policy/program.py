"""Production-shaped digest-profile classifier."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_profile(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("magic_binary"):
        return "binary"
    family = str(state.get("family", "text"))
    if family in {"pytest", "lint", "search", "log", "json", "build"}:
        return family
    return "text"

# EVOLVE-BLOCK-END
