"""Initial reviewed candidate for cold-context archive routing."""


# EVOLVE-BLOCK-START


def choose_archive_tier(state: dict, options: tuple = ()) -> str:
    if not state.get("exact_evidence_secured") or not state.get("exact_retrieval_address"):
        return "inline_text"
    provider = str(state.get("provider", "")).lower()
    frames = max(1, int(state.get("estimated_frames", 1) or 1))
    visual_safe = (
        state.get("host_image_capable")
        and provider in {"anthropic", "google", "openai"}
        and state.get("at_compaction_boundary")
        and state.get("quiet_needle_gate")
        and state.get("structure_recall_gate")
        and not state.get("contains_secrets")
        and int(state.get("provider_image_budget", 0) or 0) >= frames
    )
    return "visual_cold" if visual_safe and int(state.get("cold_tokens", 0) or 0) >= 24_000 else "address_only"


# EVOLVE-BLOCK-END
