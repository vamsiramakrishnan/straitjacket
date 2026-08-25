"""Seed policy for cost-aware host/model route selection."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START


def choose_route(
    task: Mapping[str, Any], routes: Sequence[Mapping[str, Any]]
) -> str:
    """Return one route ID expected to complete the task at low total cost."""
    complexity = int(task.get("complexity", 3))
    risk = str(task.get("risk", "medium"))
    needs_review = bool(task.get("needs_review"))
    context_needed = int(task.get("context_tokens", 0))

    viable = [
        route
        for route in routes
        if int(route.get("capability", 0)) >= complexity
        and int(route.get("context_window", 0)) >= context_needed
    ]
    if not viable:
        viable = list(routes)

    if risk == "high" or needs_review:
        reviewed = [route for route in viable if route.get("review")]
        if reviewed:
            viable = reviewed
    elif complexity >= 4:
        planned = [route for route in viable if route.get("planning")]
        if planned:
            viable = planned

    def expected_cost(route: Mapping[str, Any]) -> float:
        dollars = float(route.get("dollars", 0.0))
        repair_turns = float(route.get("repair_turns", 0.0))
        tokens = float(route.get("visible_tokens", 0.0))
        return dollars * (1.0 + 0.35 * repair_turns) + tokens / 2_000_000.0

    return str(min(viable, key=lambda route: (expected_cost(route), str(route["id"]))) ["id"])

# EVOLVE-BLOCK-END
