"""The steward: typed failure classification and the promoted recovery policy.

Two things are pinned. First, the classifier's judgement calls — the mappings
that decide whether a stronger model gets spent — including the deliberate
one that keeps a one-shot host's own execution denial escalatable. Second,
that the steward only ever offers the recovery policy actions that exist for
the node in front of it, and that its decision labels are honest.
"""

import pytest

from ctx import hosts
from ctx.recovery_policy import choose_recovery
from ctx.steward import Classification, classify_failure, de_escalation_target, decide, escalation_target


def _hosts(*installed):
    def which(b):
        return f"/usr/bin/{b}" if b in installed else None
    return [d for d in hosts.detect_all(which=which) if d.installed and d.harnessable]


def _cls(**kw):
    base = dict(code=1, stdout="", stderr="", turns=0, attempt=1, expected_turns=12,
                contract_failed=True)
    base.update(kw)
    return classify_failure(**base)


@pytest.mark.parametrize("code,out,err,expected", [
    (0, "did the thing at repo:x.py:1", "", ("done", "none")),
    (1, "", "Error: not logged in", ("blocked", "auth_failure")),
    (1, "", "HTTP 401 unauthorized", ("blocked", "auth_failure")),
    (1, "request refused by policy", "", ("blocked", "safety_denied")),
    (1, "", "429 too many requests", ("failed", "rate_limited")),
    (127, "", "OSError: no such file", ("failed", "transient_transport")),
    (1, "", "TimeoutExpired", ("failed", "transient_transport")),
    (1, "", "boom", ("failed", "capability_limit")),
])
def test_classifier_reads_the_host_vocabulary(code, out, err, expected):
    contract_failed = not (code == 0 and out)
    got = _cls(code=code, stdout=out, stderr=err, contract_failed=contract_failed)
    assert (got.reason, got.failure_kind) == expected


@pytest.mark.parametrize("text", [
    "jetski: no output produced; permission auto-denied",
    "Blocked by the read-only workspace: no source or test files could be modified.",
])
def test_a_hosts_own_execution_denial_stays_escalatable(text):
    """The one-shot session denied ITSELF a tool, or ran read-only. Another host
    routinely succeeds, and the orchestrator's acceptance test pins that. So
    this is a capability-class failure, not a permission the task lacks."""
    got = _cls(code=0, stdout=text, contract_failed=True)
    assert got == Classification("blocked", "capability_limit")
    assert choose_recovery({"failure_kind": got.failure_kind, "attempts": 1,
                            "budget_remaining_usd": 1.0,
                            "actions": ({"id": "escalate", "cost_usd": 0.1},
                                        {"id": "stop_blocked", "cost_usd": 0})}) == "escalate"


def test_incomplete_contract_becomes_repeated_on_the_second_attempt():
    first = _cls(code=0, stdout="The task is NOT COMPLETE", attempt=1)
    again = _cls(code=0, stdout="The task is NOT COMPLETE", attempt=2)
    assert first.failure_kind == "incomplete_contract"
    assert again.failure_kind == "repeated_incomplete"


def test_over_turns_is_the_feedback_signal():
    """Past the claimed turn count, the node has told us its complexity was
    underestimated: the answer is a stronger model or a stop, never the same
    model again."""
    got = _cls(code=1, stderr="boom", turns=20, expected_turns=12)
    assert got.reason == "over_turns" and got.failure_kind == "capability_limit"
    assert _cls(code=1, stderr="boom", turns=20, expected_turns=0).reason == "failed"  # 0 = observe only


def test_escalation_target_is_the_cheapest_one_tier_up():
    H = _hosts("claude", "codex")
    claude = next(h for h in H if h.name == "claude")
    economy = next(m for m in claude.models if m.tier == "economy")
    host, model = escalation_target(economy, H)
    assert hosts.tier_rank(model.tier) == hosts.tier_rank(economy.tier) + 1
    frontier = next(m for m in claude.models if m.tier == "frontier")
    assert escalation_target(frontier, H) is None


def _decide(cls, **kw):
    H = _hosts("claude", "codex")
    claude = next(h for h in H if h.name == "claude")
    economy = next(m for m in claude.models if m.tier == "economy")
    base = dict(classification=cls, attempt=1, max_attempts=2, budget_remaining_usd=float("inf"),
                est_cost_usd=0.05, cur_model=economy, hosts=H, replan_available=False)
    base.update(kw)
    return decide(**base)


def test_capability_limit_escalates_when_something_stronger_exists():
    d = _decide(Classification("failed", "capability_limit"))
    assert d.action == "escalate" and d.target is not None and "/" in d.target_name
    assert "escalate" in d.menu and "retry_same" in d.menu


def test_nothing_stronger_installed_is_stop_blocked_not_stop_budget():
    """The policy's own fall-through is stop_budget. With money to spare that
    label would be false in the receipt."""
    H = _hosts("claude")
    claude = H[0]
    frontier = next(m for m in claude.models if m.tier == "frontier")
    d = _decide(Classification("failed", "capability_limit"), hosts=H, cur_model=frontier)
    assert "escalate" not in d.menu
    assert d.action == "stop_blocked"


def test_auth_failure_stops_even_with_budget_and_a_stronger_model():
    d = _decide(Classification("blocked", "auth_failure"))
    assert d.action == "stop_blocked" and d.target is None


def test_transient_retries_the_same_model_once():
    assert _decide(Classification("failed", "transient_transport")).action == "retry_same"
    # second attempt: the evolved policy refuses to loop on transport
    d = _decide(Classification("failed", "transient_transport"), attempt=2, max_attempts=3)
    assert d.action != "retry_same"


def test_incomplete_contract_prefers_replan_when_a_coordinator_is_available():
    with_replan = _decide(Classification("failed", "incomplete_contract"), replan_available=True)
    without = _decide(Classification("failed", "incomplete_contract"), replan_available=False)
    assert with_replan.action == "replan"
    assert without.action == "escalate"


def test_last_attempt_offers_no_attempt_consuming_action():
    d = _decide(Classification("failed", "capability_limit"), attempt=2, max_attempts=2)
    assert "escalate" not in d.menu and "retry_same" not in d.menu
    assert d.action == "stop_blocked"


def test_no_budget_left_is_stop_budget():
    d = _decide(Classification("failed", "capability_limit"), budget_remaining_usd=0.0)
    assert d.action == "stop_budget"


def test_de_escalation_target_is_the_cheapest_tier_below_current():
    H = _hosts("claude", "codex")
    claude = next(h for h in H if h.name == "claude")
    frontier = next(m for m in claude.models if m.tier == "frontier")
    host, model = de_escalation_target(frontier, H)
    assert hosts.tier_rank(model.tier) < hosts.tier_rank(frontier.tier)
    economy = next(m for m in claude.models if m.tier == "economy")
    assert de_escalation_target(economy, H) is None   # nothing cheaper than economy
