"""Seed policy for allocating a bounded model-visible evidence budget."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START

_KIND_PRIORITY = {
    "root_cause": 120,
    "failure_identity": 110,
    "verification": 95,
    "source_coordinate": 85,
    "retrieval_address": 80,
    "terminal_summary": 75,
    "command": 60,
    "context": 35,
    "teaching": 10,
    "noise": -50,
}


def allocate_context(items: Sequence[Mapping[str, Any]], budget_tokens: int) -> list[int]:
    """Return ordered item indices whose total token cost fits the budget."""
    if budget_tokens <= 0:
        return []
    
    # Rank items using a multi-layered tuple: (not_mandatory, -density, index)
    # Since False < True, all mandatory items are automatically grouped and processed first,
    # and each group is internally sorted by density descending.
    ranked: list[tuple[bool, float, int]] = []
    for index, item in enumerate(items):
        tokens = max(1, int(item.get("tokens", 1)))
        kind = str(item.get("kind", "context"))
        
        base_score = float(_KIND_PRIORITY.get(kind, 25))
        severity = float(item.get("severity", 0))
        novelty = float(item.get("novelty", 0))
        addressable = bool(item.get("addressable", False))
        position = max(0.0, float(item.get("position", 0)))
        
        score = base_score
        score += 15.0 * severity
        score += 10.0 * novelty
        if addressable:
            score += 8.0
            
        # Apply a smooth reciprocal discount for deep/late positions
        score *= (1.0 / (1.0 + 0.005 * position))
        
        # Determine if the item is absolutely mandatory for task completion
        mandatory = bool(
            item.get("mandatory")
            or item.get("required")
            or kind in ("root_cause", "failure_identity")
            or severity >= 0.9
        )
        
        density = score / tokens
        ranked.append((not mandatory, -density, index))

    selected: list[int] = []
    spent = 0
    for not_mandatory, neg_density, index in sorted(ranked):
        cost = max(1, int(items[index].get("tokens", 1)))
        
        # If the item is optional, apply a strict density threshold to filter out low-value clutter
        if not_mandatory:
            density = -neg_density
            if density < 0.06:
                continue
                
        if spent + cost <= budget_tokens:
            selected.append(index)
            spent += cost
            
    return sorted(selected)

# EVOLVE-BLOCK-END
