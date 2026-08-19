"""Naive seed policy for ordinary, bounded coding-assistant requests."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START


def choose_fast_path(
    task: Mapping[str, Any], plans: Sequence[Mapping[str, Any]]
) -> str:
    """Choose a plan ID; the seed intentionally uses the broad naive path."""
    for plan in plans:
        if plan.get("id") == "broad_standard":
            return "broad_standard"
    return str(plans[0]["id"]) if plans else ""


# EVOLVE-BLOCK-END
