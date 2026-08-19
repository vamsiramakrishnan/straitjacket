"""Reviewed AlphaEvolve policy for orchestration mutation isolation."""

from __future__ import annotations

# EVOLVE-BLOCK-START


def choose_mutation_isolation(state: dict, options: tuple = ()) -> str:
    """Never overlap mutations unless distinct worktrees and targets are proven."""
    mutations = max(0, int(state.get("mutation_count", 0)))
    if mutations == 0:
        return "readonly_shared"
    if (
        mutations > 1
        and state.get("isolated_worktrees")
        and state.get("targets_declared")
        and not state.get("target_overlap")
    ):
        return "parallel_worktrees"
    return "serial_workspace"


# EVOLVE-BLOCK-END
