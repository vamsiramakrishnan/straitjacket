"""Safe but deliberately serial AlphaEvolve seed."""

# EVOLVE-BLOCK-START


def choose_wave(state: dict, options: tuple = ()) -> str:
    return "mutation_serial" if state.get("mutation_count") else "serial"


# EVOLVE-BLOCK-END
