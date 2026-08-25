"""Seed harness-routing policy for delegated tasks.

The ladder mirrors the collaboration doctrine: pins are authoritative, secrets
never leave the coordinator, unknown shapes stay local, cross-reviews must be
independent of the author, and only then does economy of fit decide.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START

_PREFERENCE = {
    "implement": ("codex", "claude", "gemini"),
    "review": ("gemini", "claude", "codex"),
    "docs": ("gemini", "codex", "claude"),
    "triage": ("gemini", "codex", "claude"),
}


def choose_harness(state: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> str:
    """Return one option id: which harness executes this task."""
    available = {str(option.get("id", "")) for option in options}
    task = state

    pinned = str(task.get("pinned") or "")
    if pinned and pinned in available:
        return pinned
    if task.get("secret_material"):
        return "coordinator" if "coordinator" in available else sorted(available)[0]

    kind = str(task.get("task_kind") or "unknown")
    if kind == "unknown" or not task.get("shape_known"):
        return "coordinator" if "coordinator" in available else sorted(available)[0]

    candidates = list(_PREFERENCE.get(kind, ()))
    if kind == "review":
        author = str(task.get("author_host") or "")
        candidates = [name for name in candidates if name != author]

    for name in candidates:
        if name in available:
            return name
    return "coordinator" if "coordinator" in available else sorted(available)[0]

# EVOLVE-BLOCK-END
