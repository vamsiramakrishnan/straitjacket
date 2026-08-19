"""Conservative AlphaEvolve verification seed."""

# EVOLVE-BLOCK-START


def choose_verification(state: dict, options: tuple = ()) -> str:
    if state.get("high_risk"):
        return "independent_standard" if state.get("alternate_host") else "focused_standard"
    if state.get("mutation") and state.get("alternate_host"):
        return "independent_economy"
    return "focused_economy"


# EVOLVE-BLOCK-END
