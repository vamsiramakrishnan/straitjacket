"""Safe serial-workspace AlphaEvolve seed."""

# EVOLVE-BLOCK-START


def choose_mutation_isolation(state: dict, options: tuple = ()) -> str:
    if not state.get("mutation_count"):
        return "readonly_shared"
    return "serial_workspace"


# EVOLVE-BLOCK-END
