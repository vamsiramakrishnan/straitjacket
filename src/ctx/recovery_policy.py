"""Reviewed AlphaEvolve policy for what to do when a node does not complete.

Promoted from ``evals/alphaevolve/escalation_policy/program.py`` — the seed
that lever ``recovery-escalation`` was evolved against, registered with seam
``src/ctx/orchestrator.py:run_route`` and never wired in. Until now that seam
ran a fixed rule: any failure escalates one tier up, once. The evolved policy
is typed: it reads *why* the node stopped and chooses the cheapest action that
can actually fix it, including the honest ones the fixed rule could not express
— retry a transient, stop on a denial no stronger model can repair, stop when
the budget cannot cover the repair.

The body below is the seed verbatim. The experiment directory keeps its own
copy as the evolution baseline; this is the production seam, imported by the
steward. Same shape as the other four promoted policies (handoff, mutation,
verification, wave): one pure function, one EVOLVE-BLOCK, no I/O.

Action ids and ``failure_kind`` values are the vocabulary the evaluator's
search / holdout / adversarial cases are written in; the steward translates a
host's actual exit, output and usage into that vocabulary before calling this.
"""

from __future__ import annotations

from typing import Any, Mapping

# EVOLVE-BLOCK-START


def choose_recovery(state: Mapping[str, Any]) -> str:
    actions = {str(action["id"]): action for action in state.get("actions", ())}
    failure = str(state.get("failure_kind", "unknown"))
    attempts = int(state.get("attempts", 1))
    budget = float(state.get("budget_remaining_usd", 0.0))

    preferences: list[str]
    if failure in {"permission_denied", "auth_failure", "safety_denied"}:
        preferences = ["stop_blocked"]
    elif failure in {"missing_evidence", "context_omission"}:
        preferences = ["focused_retrieve", "replan"]
    elif failure in {"incomplete_contract", "verification_failure"}:
        preferences = ["replan", "escalate"]
    elif failure in {"transient_transport", "rate_limited"} and attempts < 2:
        preferences = ["retry_same", "replan"]
    elif failure in {"capability_limit", "repeated_incomplete"}:
        preferences = ["escalate", "replan"]
    else:
        preferences = ["replan", "escalate", "stop_blocked"]

    for action_id in preferences:
        action = actions.get(action_id)
        if action is not None and float(action.get("cost_usd", 0.0)) <= budget:
            return action_id
    if "stop_budget" in actions:
        return "stop_budget"
    return min(actions) if actions else ""


# EVOLVE-BLOCK-END
