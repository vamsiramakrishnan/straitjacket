"""Seed policy for allocating a bounded model-visible evidence budget."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START

_KIND_PRIORITY = {
    "root_cause": 100,
    "failure_identity": 95,
    "verification": 90,
    "source_coordinate": 85,
    "retrieval_address": 80,
    "terminal_summary": 72,
    "command": 55,
    "context": 35,
    "teaching": 15,
    "noise": 0,
}


def allocate_context(items: Sequence[Mapping[str, Any]], budget_tokens: int) -> list[int]:
    """Return ordered item indices whose total token cost fits the budget."""
    if budget_tokens <= 0:
        return []
    ranked: list[tuple[float, int]] = []
    for index, item in enumerate(items):
        tokens = max(1, int(item.get("tokens", 1)))
        score = float(_KIND_PRIORITY.get(str(item.get("kind", "context")), 25))
        score += 12.0 * float(item.get("severity", 0))
        score += 8.0 * float(item.get("novelty", 0))
        if item.get("addressable"):
            score += 6.0
        ranked.append((-(score / tokens), index))

    selected: list[int] = []
    spent = 0
    for _density, index in sorted(ranked):
        cost = max(1, int(items[index].get("tokens", 1)))
        if spent + cost <= budget_tokens:
            selected.append(index)
            spent += cost
    return sorted(selected)

# EVOLVE-BLOCK-END
