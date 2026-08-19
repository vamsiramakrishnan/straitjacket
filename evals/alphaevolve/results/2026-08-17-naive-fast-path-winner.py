"""Quarantined AlphaEvolve output; not production code.

Search and holdout passed on 2026-08-17. Adversarial evaluation failed because
substring matching classifies "latest" and "testimony" as test requests.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START


def choose_fast_path(
    task: Mapping[str, Any], plans: Sequence[Mapping[str, Any]]
) -> str:
    """Choose a plan ID from inferred requirements and plan capabilities."""
    if not plans:
        return ""

    is_test = False
    for key, value in task.items():
        if "test" in key.lower() and value:
            is_test = True
            break
        if isinstance(value, str) and "test" in value.lower():
            is_test = True
            break

    if task.get("changes_present"):
        required = {"diff", "answer"}
    elif task.get("mutation"):
        if task.get("target_known"):
            required = {"read", "edit", "verify"}
        else:
            required = {"search", "read", "edit", "verify"}
    elif is_test:
        required = {"test"}
    elif task.get("target_known"):
        required = {"read", "answer"}
    elif task.get("provided_context"):
        required = {"answer"}
    else:
        required = {"search", "read", "answer"}

    capability_costs = {
        "answer": 1,
        "read": 2,
        "diff": 3,
        "test": 4,
        "verify": 2,
        "edit": 5,
        "search": 10,
    }

    def plan_cost(plan: Mapping[str, Any]) -> float:
        for key in ("cost", "estimated_cost", "price"):
            if key in plan and plan[key] is not None:
                return float(plan[key])
        return sum(
            capability_costs.get(capability, 5)
            for capability in plan.get("capabilities", [])
        )

    best_plan = None
    best_score = (float("inf"), float("inf"), float("inf"))
    for plan in plans:
        capabilities = set(plan.get("capabilities", []))
        uncovered = len(required - capabilities)
        is_broad = 1 if plan.get("id") == "broad_standard" else 0
        score = (uncovered, plan_cost(plan), is_broad)
        if score < best_score:
            best_score = score
            best_plan = plan

    return str(best_plan["id"]) if best_plan else str(plans[0]["id"])


# EVOLVE-BLOCK-END
