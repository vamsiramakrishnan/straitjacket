"""The closed loop over the task ledger: claims, handbacks, steward, resume.

These exercise `run_route` / `orchestrate` with injected launchers, the same
way the existing orchestrator tests do, and pin what the ledger adds: every
launch is claimed and handed back, the steward's decision is on record before
it is acted on, a killed run resumes without re-doing finished work, an inbox
address reaches its node, and the budget is checked against what the hosts
actually charged.
"""

import json

import pytest

from conftest import make_store, make_ws
from ctx import hosts
from ctx import taskledger as L
from ctx.orchestrator import (
    _launch_host,
    _plan_from_ledger,
    build_route_plan,
    orchestrate,
    run_route,
)
from ctx.sessiondir import session_reads_path


def _hosts(*installed):
    def which(b):
        return f"/usr/bin/{b}" if b in installed else None
    return [d for d in hosts.detect_all(which=which) if d.installed and d.harnessable]


_RAW = {"nodes": [
    {"id": "explore", "goal": "look around", "min_tier": "economy", "deps": []},
    {"id": "implement", "goal": "make the change", "min_tier": "economy", "deps": ["explore"]},
    {"id": "verify", "goal": "prove it", "min_tier": "economy", "deps": ["implement"]},
]}


def _usage(cost=0.01, turns=2):
    return {"input_tokens": 10, "output_tokens": 5, "cost_usd": cost, "turns": turns}


def _ok(host, root, prompt, exe, *, timeout, model=""):
    return 0, f"{host.name} ok at repo:x.py:1", "", _usage()


def test_every_launch_is_claimed_and_handed_back_and_no_task_text_leaks(state_home, git_workspace):
    ws = make_ws(git_workspace)
    secret = "Fix the SECRET-CUSTOMER-NAME typo in README.md"
    plan = build_route_plan(secret, _RAW, _hosts("claude"), ws.config.orchestrate)
    result = run_route(ws, plan, ws.config.orchestrate, launch=_ok)

    rows = L.load(ws.root, result.task_id)
    kinds = [r["schema"] for r in rows]
    assert kinds[0] == L.TASK_SCHEMA
    assert kinds.count(L.CLAIM_SCHEMA) == 3 and kinds.count(L.HANDBACK_SCHEMA) == 3
    st = L.task_state(rows)
    assert all(n.done for n in st.nodes.values())
    assert st.spent_usd == pytest.approx(0.03) and st.cost_complete and st.turns == 6

    raw = L.ledger_path(ws.root, result.task_id).read_text(encoding="utf-8")
    assert "SECRET-CUSTOMER-NAME" not in raw and "look around" not in raw
    # ...but the goal is one address away, in the store.
    store = make_store(ws, state_home)
    doc = json.loads(store.get_blob(st.task["goal_ref"].removeprefix("blob:")))
    assert doc["task"] == secret and doc["nodes"]["explore"] == "look around"

    receipt = json.loads(session_reads_path(ws.root, "route.jsonl").read_text().splitlines()[-1])
    assert receipt["task_id"] == result.task_id
    assert receipt["measurement"]["ledger_spend_usd"] == pytest.approx(0.03)
    assert receipt["outcomes"][0]["reason"] == "done" and receipt["outcomes"][0]["attempts"] == 1


def test_steward_decision_is_on_record_before_the_escalation_runs(state_home, git_workspace):
    ws = make_ws(git_workspace)
    plan = build_route_plan("t", {"nodes": [_RAW["nodes"][0]]}, _hosts("claude", "codex"),
                            ws.config.orchestrate)

    def launch(host, root, prompt, exe, *, timeout, model=""):
        if "haiku" in model or model.endswith("luna"):
            return 1, "", "boom", _usage(cost=0.02)
        return 0, "recovered", "", _usage(cost=0.20)

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    o = result.outcomes[0]
    assert o.status == "ok" and o.escalated_to and o.attempts == 2
    assert o.reason == "done" and o.steward_action == "escalate"
    st = L.task_state(L.load(ws.root, result.task_id))
    assert [s["action"] for s in st.steward] == ["escalate"]
    assert st.steward[0]["failure_kind"] == "capability_limit"
    assert st.steward[0]["target"] == o.escalated_to
    assert st.nodes["explore"].cost_usd == pytest.approx(0.22)  # both attempts billed


def test_auth_failure_stops_instead_of_buying_a_stronger_model(state_home, git_workspace):
    """The fixed rule this replaces would have escalated. The promoted policy
    knows a login problem is not a capability problem."""
    ws = make_ws(git_workspace)
    plan = build_route_plan("t", {"nodes": [_RAW["nodes"][0]]}, _hosts("claude", "codex"),
                            ws.config.orchestrate)
    calls = {"n": 0}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        calls["n"] += 1
        return 1, "", "Error: not logged in. Run `claude login`.", _usage(cost=0.001)

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    o = result.outcomes[0]
    assert calls["n"] == 1 and o.status == "failed"
    assert o.reason == "blocked" and o.failure_kind == "auth_failure"
    assert o.steward_action == "stop_blocked" and o.escalated_to is None


def test_transient_failure_retries_the_same_model(state_home, git_workspace):
    ws = make_ws(git_workspace)
    plan = build_route_plan("t", {"nodes": [_RAW["nodes"][0]]}, _hosts("claude"),
                            ws.config.orchestrate)
    seen = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        seen.append(model)
        return (127, "", "OSError: spawn failed") if len(seen) == 1 else (0, "ok", "", _usage())

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    assert result.outcomes[0].status == "ok"
    assert len(seen) == 2 and seen[0] == seen[1]          # same model, not a tier up
    assert result.outcomes[0].steward_action == "retry_same"


def test_over_turns_escalates_as_a_complexity_signal(state_home, git_workspace):
    ws = make_ws(git_workspace)
    plan = build_route_plan("t", {"nodes": [_RAW["nodes"][0]]}, _hosts("claude"),
                            ws.config.orchestrate)

    def launch(host, root, prompt, exe, *, timeout, model=""):
        if "haiku" in model:
            return 1, "", "gave up", _usage(turns=40)   # past expected_turns=12
        return 0, "ok", "", _usage()

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    o = result.outcomes[0]
    assert o.status == "ok" and o.escalated_to
    st = L.task_state(L.load(ws.root, result.task_id))
    first = [r for r in L.load(ws.root, result.task_id) if r["schema"] == L.HANDBACK_SCHEMA][0]
    assert first["reason"] == "over_turns" and first["turns"] == 40
    assert st.steward[0]["on_reason"] == "over_turns"


def test_budget_is_checked_against_actuals_not_the_estimate(state_home, git_workspace):
    """Each attempt is priced far above its estimate. The estimate-only loop
    would have kept going; the ledger stops it."""
    from dataclasses import replace

    ws = make_ws(git_workspace)
    est = build_route_plan("t", _RAW, _hosts("claude"), ws.config.orchestrate).est_total_usd
    budget = round(est * 1.3, 4)               # the estimate says the whole route fits...
    cfg = replace(ws.config.orchestrate, budget_usd=budget)
    plan = build_route_plan("t", _RAW, _hosts("claude"), cfg)
    # ...but the first node bills enough that what is left cannot cover the
    # SECOND node's own estimate, which is what the claim check compares.
    second_est = plan.assigned[1].est_cost_usd
    per_node = round(budget - second_est + 0.001, 4)
    assert 0 < per_node < budget

    def launch(host, root, prompt, exe, *, timeout, model=""):
        return 0, "ok", "", _usage(cost=per_node)

    result = run_route(ws, plan, cfg, launch=launch)
    statuses = [o.status for o in result.outcomes]
    # After one node 60% is spent; the second node's ESTIMATE exceeds what is
    # left, so it is refused at the claim -- never launched -- and its
    # dependent is skipped. Spend stays inside the budget.
    assert statuses == ["ok", "failed", "skipped"]
    second = result.outcomes[1]
    assert second.reason == "over_budget" and second.steward_action == "stop_budget"
    assert second.attempts == 0 and second.exit_code is None
    assert result.ledger_spend_usd == pytest.approx(per_node) and result.ledger_spend_usd <= budget
    assert result.ledger_spend_complete
    st = L.task_state(L.load(ws.root, result.task_id))
    assert [s["action"] for s in st.steward] == ["stop_budget"]
    assert sum(1 for r in L.load(ws.root, result.task_id) if r["schema"] == L.CLAIM_SCHEMA) == 1
    assert result.estimated_spend_usd < budget  # the estimate alone would have continued


def test_resume_restores_finished_nodes_and_delivers_inbox(state_home, git_workspace, monkeypatch):
    ws = make_ws(git_workspace)
    # `orchestrate` detects hosts itself; CI runners have no harness CLI on PATH.
    monkeypatch.setattr("ctx.orchestrator.installed_harnessable", lambda **kw: _hosts("claude"))
    plan = build_route_plan("resumable task", _RAW, _hosts("claude"), ws.config.orchestrate)

    class Crash(RuntimeError):
        pass

    calls = {"n": 0}

    def crashing(host, root, prompt, exe, *, timeout, model=""):
        calls["n"] += 1
        if "implement" in prompt:
            raise Crash("orchestrator died mid-wave")
        return 0, f"{host.name} ok", "", _usage()

    with pytest.raises(Crash):
        run_route(ws, plan, ws.config.orchestrate, launch=crashing)
    tid = L.list_tasks(ws.root)[0]
    st = L.task_state(L.load(ws.root, tid))
    assert st.nodes["explore"].done and not st.nodes["implement"].done

    # Someone hands the implementer an address while the run is down.
    L.append(ws.root, L.inbox_row(tid, to="implement", sender="operator",
                                  ref="repo:README.md --lines 1:3", note="start from the title"))
    prompts = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        prompts.append(prompt)
        return 0, f"{host.name} ok", "", _usage()

    code, text = orchestrate(ws, "", launch=launch, resume=tid)
    assert code == 0
    assert "resumed from ledger: explore" in text
    assert not any("look around" in p for p in prompts)        # explore was NOT re-run
    assert any("repo:README.md --lines 1:3" in p and "start from the title" in p
               for p in prompts)                                  # inbox reached implement
    st = L.task_state(L.load(ws.root, tid))
    assert all(n.done for n in st.nodes.values())
    assert st.nodes["explore"].attempts == 1                      # still one attempt, ever


def test_resume_of_an_unknown_task_fails_loudly(state_home, git_workspace, monkeypatch):
    ws = make_ws(git_workspace)
    monkeypatch.setattr("ctx.orchestrator.installed_harnessable", lambda **kw: _hosts("claude"))
    code, text = orchestrate(ws, "", launch=_ok, resume="task-000000000000")
    assert code == 1 and "cannot resume" in text


def test_plan_from_ledger_reroutes_a_pinned_host_that_is_gone(state_home, git_workspace):
    ws = make_ws(git_workspace)
    plan = build_route_plan("t", _RAW, _hosts("claude", "codex"), ws.config.orchestrate)
    result = run_route(ws, plan, ws.config.orchestrate, launch=_ok)
    # Later, only claude is installed.
    rebuilt, task = _plan_from_ledger(ws, result.task_id, _hosts("claude"))
    assert task == "t"
    assert {a.host.name for a in rebuilt.assigned} == {"claude"}
    assert [a.node.id for a in rebuilt.assigned] == ["explore", "implement", "verify"]


def test_turn_ceiling_hard_bounds_a_claude_node_at_launch(monkeypatch, tmp_path):
    host = next(h for h in _hosts("claude") if h.name == "claude")
    seen = {}

    class Completed:
        returncode = 0
        stdout = json.dumps({"result": "ok", "num_turns": 3,
                             "usage": {"input_tokens": 10, "output_tokens": 4}})
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Completed()

    monkeypatch.setattr("ctx.orchestrator.subprocess.run", fake_run)
    code, out, err, usage = _launch_host(host, tmp_path, "do it", "/usr/bin/ctx",
                                         timeout=5, model="claude-haiku-4.5", max_turns=7)
    assert "--max-turns" in seen["argv"] and "7" in seen["argv"]
    assert usage.turns == 3
    _launch_host(host, tmp_path, "do it", "/usr/bin/ctx", timeout=5, model="claude-haiku-4.5")
    assert "--max-turns" not in seen["argv"]   # 0 = observe only, nothing added
