"""Production-shaped repository-context policy."""

from typing import Any, Mapping, Sequence

# EVOLVE-BLOCK-START

def choose_context(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    if state.get("unknown_architecture"):
        return "full_repo"
    if state.get("named_file"):
        return "named_file"
    if state.get("symbol"):
        return "symbol_neighborhood"
    if state.get("changes"):
        return "changed_files"
    if state.get("language"):
        return "scoped_corpus"
    return "full_repo"

# EVOLVE-BLOCK-END
