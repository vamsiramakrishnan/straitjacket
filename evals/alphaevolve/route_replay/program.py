"""Production-shaped seed for receipt-informed route compilation."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START


def choose_route(
    profile: Mapping[str, Any], routes: Sequence[Mapping[str, Any]]
) -> str:
    """Choose the smallest production route that preserves completion."""
    kind = str(profile.get("kind", "general"))
    if (
        kind == "general"
        and profile.get("named_target")
        and profile.get("named_acceptance")
        and profile.get("explicit_contract")
        and not profile.get("high_risk_scope")
    ):
        return "lean_explicit_feature"
    preferred = {
        "answer": "focused_answer",
        "inspect": "focused_answer",
        "review": "focused_review",
        "test": "proven_unattended_test",
        "simple_edit": "focused_edit_verify",
    }.get(kind, "complete_general")
    for route in routes:
        if route.get("id") == preferred:
            return preferred
    return "complete_general"


# EVOLVE-BLOCK-END
