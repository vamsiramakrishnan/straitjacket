"""The steward: the deterministic reader of handbacks.

When a node stops without finishing, something has to decide what happens
next. Before the task ledger that decision was one fixed line in ``run_route``:
escalate one tier up, once, whatever went wrong. That rule spends a stronger
model on an expired login, retries nothing that was merely transient, and has
no word for "the budget cannot cover the fix".

The steward replaces it with two pure functions:

* :func:`classify_failure` turns what a host actually returned — exit code,
  output, usage, the attempt number — into a handback ``reason`` and one of
  the typed ``failure_kind`` values the recovery policy was evolved against.
* :func:`decide` builds the menu of actions that *exist for this node right
  now*, each with a real cost, and asks the promoted
  :func:`ctx.recovery_policy.choose_recovery` which to take.

It is not a model. It never launches anything. It reads ledger state (budget
against actuals, attempts so far), returns a decision, and the orchestrator
records that decision as a ``ctx.steward/v1`` row before acting on it — so
every escalation, retry and stop is in the receipt with the reason it was
chosen. That is the layer omnigent calls its server and openrig its daemon:
stateful policy that sits between harnesses and outlives any one of them.
Here it is a function over a JSONL file, because that is all it needs to be.

## Why the classifier is where the judgement lives

The recovery policy is only as good as the ``failure_kind`` it is handed, and
the mapping from host output to kind is the one place a wrong call is
expensive in both directions. Two of the mappings are deliberate and worth
stating:

* A one-shot host that reports ``permission auto-denied`` or ``read-only
  workspace`` has hit *its own* execution mode, not a permission the task
  lacks. Another host, or the same one in another mode, routinely succeeds —
  the existing acceptance test pins exactly that. So those are
  ``capability_limit`` (escalate), not ``permission_denied`` (stop).
* ``auth_failure`` is reserved for wording that names credentials: not logged
  in, unauthorized, expired, invalid key. Nothing a stronger model fixes, so
  the policy stops rather than spends — the honest outcome the fixed rule
  could not produce.

Turns are the third input and the newest. A node past its claimed turn count
has told us its complexity was underestimated; that becomes ``over_turns``
with a capability-class kind, so the policy's answer is a stronger model or
an honest stop, never the same model again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ctx.hosts import tier_rank
from ctx.recovery_policy import choose_recovery

#: Host wording that means "this one-shot session denied or could not perform
#: the action" — an execution-mode failure another host can clear.
_EXECUTION_DENIED = (
    "auto-denied",
    "no output produced",
    "read-only workspace",
    "no source or test files could be modified",
    "unable to make changes due to workspace constraints",
)

#: Host wording that means the node ended without meeting its contract.
_INCOMPLETE = (
    "task is not complete",
    "implementation incomplete",
    "not complete",
)

#: Wording that names credentials. A stronger model cannot fix these.
_AUTH = (
    "not logged in", "unauthorized", "unauthenticated", "invalid api key",
    "authentication", "login required", "token expired", "credential",
    "401",
)

#: Wording that names a policy/safety refusal by the host or provider.
_SAFETY = ("safety", "content policy", "refused by policy", "blocked by policy")

#: Wording that names a provider rate limit.
_RATE = ("rate limit", "rate-limit", "429", "overloaded", "too many requests")

#: Wording for a transport or process failure, retryable once.
_TRANSPORT = (
    "oserror", "subprocesserror", "timeoutexpired", "connection reset",
    "connection refused", "econnreset", "broken pipe", "timed out",
)


@dataclass(frozen=True)
class Classification:
    reason: str        # a taskledger.HANDBACK_REASONS member
    failure_kind: str  # a taskledger.FAILURE_KINDS member


def classify_failure(
    *,
    code: int,
    stdout: str,
    stderr: str,
    turns: int,
    attempt: int,
    expected_turns: int,
    contract_failed: bool,
) -> Classification:
    """Map one host attempt to (reason, failure_kind). Total: always returns.

    ``contract_failed`` is the orchestrator's existing zero-exit contract
    check, passed in rather than recomputed so the two can never disagree
    about what "finished" means.
    """
    text = "\n".join((stdout or "", stderr or "")).lower()
    repeated = attempt > 1

    if code == 0 and not contract_failed:
        return Classification("done", "none")

    if any(m in text for m in _AUTH):
        return Classification("blocked", "auth_failure")
    if any(m in text for m in _SAFETY):
        return Classification("blocked", "safety_denied")
    if any(m in text for m in _RATE):
        return Classification("failed", "rate_limited")

    over_turns = expected_turns > 0 and turns > expected_turns
    if over_turns:
        return Classification(
            "over_turns", "repeated_incomplete" if repeated else "capability_limit"
        )

    if code == 0 and contract_failed:
        if any(m in text for m in _EXECUTION_DENIED):
            return Classification("blocked", "capability_limit")
        if any(m in text for m in _INCOMPLETE) or not text.strip():
            return Classification(
                "failed", "repeated_incomplete" if repeated else "incomplete_contract"
            )
        return Classification("failed", "incomplete_contract")

    if code == 127 or any(m in text for m in _TRANSPORT):
        return Classification("failed", "transient_transport")

    return Classification(
        "failed", "repeated_incomplete" if repeated else "capability_limit"
    )


def de_escalation_target(cur_model, hosts):
    """The cheapest (host, model) strictly less capable than ``cur_model``,
    across every installed unattended harness -- the mirror of
    :func:`escalation_target`, for prewalk's success-driven handoff to a
    cheaper model rather than a failure-driven escalation to a stronger one.
    ``None`` when nothing cheaper is installed."""
    unattended = [h for h in hosts if h.spec.unattended]
    cheaper = [
        (h, m) for h in unattended if h.installed for m in h.models
        if tier_rank(m.tier) < tier_rank(cur_model.tier)
    ]
    if not cheaper:
        return None
    return sorted(
        cheaper,
        key=lambda hm: (tier_rank(hm[1].tier), hm[0].model_price(hm[1].id).output, hm[0].name),
    )[0]


def escalation_target(cur_model, hosts):
    """The cheapest (host, model) strictly more capable than ``cur_model``,
    across every installed unattended harness. ``None`` when nothing stronger
    is installed — the policy then cannot be offered ``escalate`` at all."""
    unattended = [h for h in hosts if h.spec.unattended]
    better = [
        (h, m) for h in unattended if h.installed for m in h.models
        if tier_rank(m.tier) > tier_rank(cur_model.tier)
    ]
    if not better:
        return None
    return sorted(
        better,
        key=lambda hm: (tier_rank(hm[1].tier), hm[0].model_price(hm[1].id).output, hm[0].name),
    )[0]


@dataclass(frozen=True)
class Decision:
    action: str                     # a taskledger.STEWARD_ACTIONS member
    classification: Classification
    target: tuple[Any, Any] | None  # (DetectedHost, ModelChoice) for escalate
    budget_remaining_usd: float
    menu: tuple[str, ...]           # the actions that were actually on offer

    @property
    def target_name(self) -> str | None:
        if self.target is None:
            return None
        host, model = self.target
        return f"{host.name}/{model.id}"


def _escalation_cost(est_cost_usd: float, cur_model, target) -> float:
    """Estimate the escalated attempt from the current one by output-price
    ratio; when pricing cannot separate the two, guess high (the direction
    that refuses rather than overruns)."""
    host, model = target
    try:
        old_rate = float(host.model_price(cur_model.id).output or 0.0)
        new_rate = float(host.model_price(model.id).output or 0.0)
        if old_rate > 0 and new_rate > 0:
            return est_cost_usd * (new_rate / old_rate)
    except Exception:
        pass
    return est_cost_usd * 3


def decide(
    *,
    classification: Classification,
    attempt: int,
    max_attempts: int,
    budget_remaining_usd: float,
    est_cost_usd: float,
    cur_model,
    hosts,
    replan_available: bool,
) -> Decision:
    """Build the real action menu and let the recovery policy choose.

    Only actions that exist are offered: ``escalate`` needs an installed
    stronger model, ``replan`` needs a coordinator with re-plans left, and a
    node at ``max_attempts`` is offered no attempt-consuming action at all.
    The policy never has to know what the orchestrator cannot do.
    """
    menu: list[dict[str, Any]] = [
        {"id": "stop_blocked", "cost_usd": 0.0},
        {"id": "stop_budget", "cost_usd": 0.0},
    ]
    target = None
    if attempt < max_attempts:
        menu.append({"id": "retry_same", "cost_usd": float(est_cost_usd)})
        target = escalation_target(cur_model, hosts)
        if target is not None:
            menu.append({
                "id": "escalate",
                "cost_usd": _escalation_cost(float(est_cost_usd), cur_model, target),
            })
    if replan_available:
        # A re-plan is the coordinator's call, priced at roughly one cheap
        # coordinator turn; the orchestrator's own bounds cap how many.
        menu.append({"id": "replan", "cost_usd": min(float(est_cost_usd), 0.05)})

    if budget_remaining_usd <= 0:
        action = "stop_budget"
    else:
        action = choose_recovery({
            "failure_kind": classification.failure_kind,
            "attempts": attempt,
            "budget_remaining_usd": budget_remaining_usd,
            "actions": tuple(menu),
        }) or "stop_blocked"
        # The policy's own fall-through is ``stop_budget`` — its last resort
        # when no preferred action was on the menu. With budget to spare that
        # label is false: the node stopped because nothing applicable existed
        # (no stronger model installed, no coordinator to re-plan), not
        # because money ran out. The receipt has to say which.
        if action == "stop_budget" and all(
            float(m["cost_usd"]) <= budget_remaining_usd for m in menu
        ):
            action = "stop_blocked"
    if action != "escalate":
        target = None
    return Decision(
        action=action,
        classification=classification,
        target=target,
        budget_remaining_usd=budget_remaining_usd,
        menu=tuple(m["id"] for m in menu),
    )


__all__ = [
    "Classification", "Decision", "classify_failure", "decide", "escalation_target",
    "de_escalation_target",
]
