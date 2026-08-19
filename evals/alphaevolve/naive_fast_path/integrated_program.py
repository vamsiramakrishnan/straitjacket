"""Production-shaped fast-path policy used by the portfolio promotion gates."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START


def choose_fast_path(
    task: Mapping[str, Any], plans: Sequence[Mapping[str, Any]]
) -> str:
    """Choose the narrowest plan that still carries required capabilities."""
    if task.get("provided_context"):
        return "answer_given"
    if task.get("changes_present"):
        return "focused_diff"
    if task.get("kind") == "test":
        return "focused_test"
    if task.get("mutation") and not task.get("target_known"):
        return "focused_search_verify"
    if task.get("mutation"):
        return "focused_edit_verify"
    if task.get("target_known"):
        return "focused_read"
    return "broad_standard"


# EVOLVE-BLOCK-END
