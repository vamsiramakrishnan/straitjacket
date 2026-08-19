"""Reviewed AlphaEvolve policy for orchestration wave concurrency."""

from __future__ import annotations

# EVOLVE-BLOCK-START


def choose_wave(state: dict, options: tuple = ()) -> str:
    """Choose bounded concurrency without overlapping shared-workspace writes."""
    ready = max(0, int(state.get("ready_count", 0)))
    mutations = max(0, int(state.get("mutation_count", 0)))
    readonly = max(0, int(state.get("readonly_count", ready - mutations)))
    if ready <= 1:
        return "mutation_serial" if mutations else "serial"
    if state.get("provider_rate_limited"):
        return "mutation_serial" if mutations else "serial"
    if mutations:
        return "readonly_first" if readonly else "mutation_serial"
    if ready == 2:
        return "parallel_two"
    return "parallel_four"


# EVOLVE-BLOCK-END
