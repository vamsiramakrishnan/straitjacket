"""Safety policy for an optional visual cold-context tier.

The content-addressed store remains the source of truth.  A visual archive is
only a redundant recall aid after every omitted byte has an exact retrieval
address; it is never allowed to become the sole copy of evidence.
"""

from __future__ import annotations


# EVOLVE-BLOCK-START


def choose_archive_tier(state: dict, options: tuple = ()) -> str:
    """Choose ``inline_text``, ``address_only``, or ``visual_cold``.

    ``visual_cold`` is deliberately difficult to enter.  The caller must prove
    that exact evidence is secured, the host can accept image context, the
    provider has a measured billing profile, the request is at a compaction
    boundary, and both quiet-needle and structure-recall gates passed for the
    selected renderer.  Unknown or incomplete state falls back to the lossless
    address tier.
    """
    exact_secured = bool(state.get("exact_evidence_secured"))
    addressable = bool(state.get("exact_retrieval_address"))
    if not (exact_secured and addressable):
        return "inline_text"

    cold_tokens = max(0, int(state.get("cold_tokens", 0) or 0))
    image_budget = max(0, int(state.get("provider_image_budget", 0) or 0))
    estimated_frames_raw = int(state.get("estimated_frames", 1) or 1)
    estimated_frames = estimated_frames_raw if estimated_frames_raw > 0 else 1
    provider = str(state.get("provider", "")).lower()
    provider_measured = provider in {"anthropic", "google", "openai"}
    visual_safe = (
        bool(state.get("host_image_capable"))
        and provider_measured
        and bool(state.get("at_compaction_boundary"))
        and bool(state.get("quiet_needle_gate"))
        and bool(state.get("structure_recall_gate"))
        and not bool(state.get("contains_secrets"))
        and image_budget >= estimated_frames
    )
    if visual_safe and cold_tokens >= 24_000:
        return "visual_cold"
    return "address_only"


# EVOLVE-BLOCK-END
