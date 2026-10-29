"""Naive seed policy for ordinary, bounded coding-assistant requests."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# EVOLVE-BLOCK-START


def choose_fast_path(
    task: Mapping[str, Any], plans: Sequence[Mapping[str, Any]]
) -> str:
    """Choose a plan ID; select the most specific/lowest-cost plan covering required capabilities."""
    if not plans:
        return ""

    # CRAZY IDEA part 1: Instead of accumulating capabilities with messy if statements,
    # we evaluate the exact semantic state of the task as a mutually exclusive decision tree, 
    # strictly mapping to the requirements contract!

    # Determine state variables
    t_test = task.get("test") or task.get("is_test") or task.get("test_request")
    if not t_test:
        t_test = any("test" in str(k).lower() or (isinstance(v, str) and "test" in v.lower()) for k, v in task.items())
    
    t_changes = bool(task.get("changes_present"))
    t_mutation = bool(task.get("mutation"))
    t_target = bool(task.get("target_known"))
    t_context = bool(task.get("provided_context") or task.get("supplied_context"))

    # State machine mapping guarantees the absolute minimum capabilities are requested
    if t_test:
        required_caps = {"test"}
    elif t_changes:
        required_caps = {"diff", "answer"}
    elif t_mutation:
        if t_target:
            required_caps = {"read", "edit", "verify"}
        else:
            required_caps = {"search", "read", "edit", "verify"}
    elif t_target:
        required_caps = {"read", "answer"}
    elif t_context:
        required_caps = {"answer"}
    else:
        # Fallback for unknown target without mutation, context, changes, or test
        required_caps = {"search", "read", "answer"}

    # CRAZY IDEA part 2: 
    # Use a "Quantum Superposition" Auction System. We collapse the wave function of plans
    # by computing a unified fitness energy state. We add a penalty (waste energy) for capabilities
    # we don't need, effectively favoring the narrowest possible plan that strictly dominates the requirement!
    
    winning_energy = float('inf')
    winner_id = ""

    for plan in plans:
        plan_id = plan.get("id", "")
        if not plan_id:
            continue
            
        plan_caps = set(plan.get("capabilities", []))
        
        # Hard gate: must satisfy all required capabilities (the wave function collapses to invalid if not)
        if not plan_caps.issuperset(required_caps):
            continue
            
        # Base energy derived from the number of capabilities
        # Using a slight exponential scale so broader plans are exponentially heavier
        base_energy = 1.5 ** len(plan_caps)
        
        # Explicit cost from plan data if available
        for key in ("cost", "estimated_cost", "price", "token_estimate"):
            if key in plan and isinstance(plan[key], (int, float)):
                base_energy = float(plan[key])
                break
                
        # Waste energy: penalize plans that have capabilities we don't need
        waste_energy = len(plan_caps - required_caps) * 0.1
        
        # Penalty for 'broad_standard' to ensure it's strictly a fallback
        fallback_penalty = 1000000.0 if plan_id == "broad_standard" else 0.0
        
        total_energy = base_energy + waste_energy + fallback_penalty
        
        if total_energy < winning_energy:
            winning_energy = total_energy
            winner_id = plan_id
            
    if winning_energy != float('inf') and winner_id:
        return winner_id

    # Absolute fallback if no plan strictly matches
    for plan in plans:
        if plan.get("id") == "broad_standard":
            return "broad_standard"
    return str(plans[0]["id"]) if plans else ""


# EVOLVE-BLOCK-END
