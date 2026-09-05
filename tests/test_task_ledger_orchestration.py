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
    PREWALK_SENTINEL,
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

    monkeypatch.setattr("ctx.orchestrator._run_bounded", fake_run)
    code, out, err, usage = _launch_host(host, tmp_path, "do it", "/usr/bin/ctx",
                                         timeout=5, model="claude-haiku-4.5", max_turns=7)
    assert "--max-turns" in seen["argv"] and "7" in seen["argv"]
    assert usage.turns == 3
    _launch_host(host, tmp_path, "do it", "/usr/bin/ctx", timeout=5, model="claude-haiku-4.5")
    assert "--max-turns" not in seen["argv"]   # 0 = observe only, nothing added


def test_claude_node_launch_carries_the_single_shot_notice(monkeypatch, tmp_path):
    """evals/bugbash-round17-2026-09-04.md: a node is itself a print-mode
    `claude -p` run. If it fans out to background subagents and then ends
    its turn to "wait" for them (as ScheduleWakeup's tool result implies a
    harness will wake it), print mode kills that work on its background
    ceiling. Every Claude node launch must carry the same single-shot
    warning as `ctx wrap claude`, unless the caller opted out."""
    from ctx.wrap import _SINGLE_SHOT_NOTICE

    host = next(h for h in _hosts("claude") if h.name == "claude")
    seen = {}

    class Completed:
        returncode = 0
        stdout = json.dumps({"result": "ok", "num_turns": 1, "usage": {}})
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Completed()

    monkeypatch.setattr("ctx.orchestrator._run_bounded", fake_run)
    monkeypatch.delenv("CTX_WRAP_NO_DISCIPLINE", raising=False)
    _launch_host(host, tmp_path, "do it", "/usr/bin/ctx", timeout=5)
    argv = seen["argv"]
    assert "--append-system-prompt" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == _SINGLE_SHOT_NOTICE

    monkeypatch.setenv("CTX_WRAP_NO_DISCIPLINE", "1")
    _launch_host(host, tmp_path, "do it", "/usr/bin/ctx", timeout=5)
    assert "--append-system-prompt" not in seen["argv"]


def test_replanned_nodes_survive_resume(state_home, git_workspace, monkeypatch):
    """A coordinator re-plan adds nodes in memory. They must reach the ledger
    (a second task row, source "replan") or a resume rebuilds the route from
    the opening row alone and the added nodes silently vanish."""
    ws = make_ws(git_workspace)
    monkeypatch.setattr("ctx.orchestrator.installed_harnessable", lambda **kw: _hosts("claude"))
    raw = {"nodes": [{"id": "main", "goal": "x", "min_tier": "frontier", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude"), ws.config.orchestrate)

    class Crash(RuntimeError):
        pass

    def first_run(host, root, prompt, exe, *, timeout, model=""):
        if "rebuild the index" in prompt:
            raise Crash("orchestrator died launching the re-planned node")
        return 1, "", "fail"

    def coordinate(extra):
        return {"nodes": [{"id": "recover", "goal": "rebuild the index",
                           "min_tier": "frontier", "deps": []}]}

    with pytest.raises(Crash):
        run_route(ws, plan, ws.config.orchestrate, launch=first_run, coordinate=coordinate)
    tid = L.list_tasks(ws.root)[0]
    st = L.task_state(L.load(ws.root, tid))
    assert [r.get("source") for r in st.task_rows][1:] == ["replan"]
    assert "recover" in st.nodes and not st.nodes["recover"].done

    rebuilt, _ = _plan_from_ledger(ws, tid, _hosts("claude"))
    by_id = {a.node.id: a.node for a in rebuilt.assigned}
    assert set(by_id) == {"main", "recover"}
    assert by_id["recover"].goal == "rebuild the index"

    prompts = []

    def second_run(host, root, prompt, exe, *, timeout, model=""):
        prompts.append(prompt)
        return 0, f"{host.name} ok", "", _usage()

    code, text = orchestrate(ws, "", launch=second_run, resume=tid)
    assert code == 0, text
    assert any("rebuild the index" in p for p in prompts)         # the added node ran
    st = L.task_state(L.load(ws.root, tid))
    assert st.nodes["recover"].done and st.nodes["main"].done


def test_parallel_claims_cannot_spend_the_same_dollar(state_home, git_workspace):
    """Two nodes of one wave claim concurrently. Each claim reserves its
    estimate under the ledger lock, so with budget for one of them exactly
    one launches and the other is refused at the claim, whichever thread
    gets there first."""
    import subprocess
    from dataclasses import replace

    (git_workspace / "a.txt").write_text("old a\n", encoding="utf-8")
    (git_workspace / "b.txt").write_text("old b\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt", "b.txt"], cwd=git_workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=git_workspace, check=True)
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, isolated_worktrees=True)
    raw = {"nodes": [
        {"id": "a", "goal": "edit a.txt", "role": "implement", "min_tier": "economy",
         "deps": [], "targets": ["a.txt"]},
        {"id": "b", "goal": "edit b.txt", "role": "implement", "min_tier": "economy",
         "deps": [], "targets": ["b.txt"]},
        {"id": "verify", "goal": "verify both", "role": "verify", "min_tier": "economy",
         "deps": ["a", "b"]},
    ]}
    plan = build_route_plan("update the two fixtures", raw, _hosts("claude", "codex"), cfg)
    est = {a.node.id: a.est_cost_usd for a in plan.assigned}
    # Room for either mutation on its own, never for both.
    cfg = replace(cfg, budget_usd=est["a"] + est["b"] * 0.5)
    launched = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        nid = "a" if "node 'a'" in prompt else "b"
        launched.append(nid)
        (root / f"{nid}.txt").write_text(f"new {nid}\n", encoding="utf-8")
        return 0, "worker completed", "", _usage(cost=0.001)

    result = run_route(ws, plan, cfg, launch=launch)
    assert result.wave_policies[0].endswith("/parallel_worktrees")
    by_id = {o.node_id: o for o in result.outcomes}
    assert len(launched) == 1
    ran, refused = launched[0], ("b" if launched[0] == "a" else "a")
    assert by_id[ran].status == "ok"
    assert by_id[refused].status == "failed"
    assert by_id[refused].reason == "over_budget" and by_id[refused].attempts == 0
    assert by_id["verify"].status == "skipped"
    st = L.task_state(L.load(ws.root, result.task_id))
    assert st.nodes[refused].attempts == 0                         # never claimed
    assert st.reserved_usd == 0.0                                  # nothing left in flight


def test_orchestrate_requires_a_task_or_resume(state_home, git_workspace, monkeypatch):
    ws = make_ws(git_workspace)
    monkeypatch.setattr("ctx.orchestrator.installed_harnessable", lambda **kw: _hosts("claude"))
    code, text = orchestrate(ws, "   ", launch=_ok)
    assert code == 1 and "--resume" in text


def test_resumed_node_is_not_charged_for_its_own_dead_claim(state_home, git_workspace):
    """A run that dies mid-launch leaves that node's claim open, reserving
    its estimate. On resume the node re-claims; the stale reservation is its
    own and must not make the budget refuse it."""
    from dataclasses import replace

    ws = make_ws(git_workspace)
    plan = build_route_plan("tight", _RAW, _hosts("claude"), ws.config.orchestrate)
    est = {a.node.id: a.est_cost_usd for a in plan.assigned}

    class Crash(RuntimeError):
        pass

    def crashing(host, root, prompt, exe, *, timeout, model=""):
        if "make the change" in prompt:
            raise Crash("died launching implement")
        return 0, f"{host.name} ok", "", _usage(cost=0.01)

    with pytest.raises(Crash):
        run_route(ws, plan, ws.config.orchestrate, launch=crashing)
    tid = L.list_tasks(ws.root)[0]
    st = L.task_state(L.load(ws.root, tid))
    assert st.nodes["implement"].open_claim is not None          # the dead claim
    assert st.reserved_usd == pytest.approx(est["implement"])

    # Exactly enough for implement's estimate on top of what was spent, and
    # nothing for verify. Counting the dead claim would refuse implement.
    cfg = replace(ws.config.orchestrate, budget_usd=st.spent_usd + est["implement"] + 1e-6)
    result = run_route(ws, plan, cfg, launch=_ok, task_id=tid, resume=True)
    by_id = {o.node_id: o for o in result.outcomes}
    assert by_id["explore"].status == "ok" and by_id["explore"].detail == "resumed from ledger"
    assert by_id["implement"].status == "ok" and by_id["implement"].attempts == 2
    st = L.task_state(L.load(ws.root, tid))
    assert st.nodes["implement"].open_claim is None                # handed back this time


# ------------------------------------------------------------------ prewalk

_PREWALK_RAW = {"nodes": [
    {"id": "build", "goal": "add the feature", "role": "implement",
     "min_tier": "frontier", "deps": []},
    {"id": "verify", "goal": "prove it", "role": "verify",
     "min_tier": "economy", "deps": ["build"]},
]}


def test_prewalk_hands_off_a_frontier_mutation_node_to_a_cheaper_model(
    state_home, git_workspace
):
    """A frontier model on a mutation node plans, edits once, and signals;
    the SAME node's next attempt runs on the cheapest installed model below
    frontier, inheriting the plan and the first edit -- never re-exploring."""
    from dataclasses import replace

    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, prewalk=True)
    plan = build_route_plan("ship it", _PREWALK_RAW, _hosts("claude"), cfg)
    assigned = plan.assigned[0]
    assert assigned.model.tier == "frontier"   # sanity: prewalk's target shape

    prompts = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        prompts.append(prompt)
        if "node \'build\'" in prompt and model == assigned.model.launch_id:
            return (
                0,
                f"Plan:\n1. do X\n2. do Y\n(edited foo.py)\n{PREWALK_SENTINEL}",
                "", _usage(cost=0.30, turns=6),
            )
        return 0, "done", "", _usage(cost=0.02, turns=3)

    result = run_route(ws, plan, cfg, launch=launch)
    by_id = {o.node_id: o for o in result.outcomes}
    outcome = by_id["build"]
    assert outcome.status == "ok" and outcome.attempts == 2
    assert outcome.reason == "done" and outcome.failure_kind == "none"
    assert outcome.escalated_to and outcome.escalated_to != f"{assigned.host.name}/{assigned.model.id}"
    assert by_id["verify"].status == "ok"

    build_prompts = [p for p in prompts if "node \'build\'" in p]
    assert len(build_prompts) == 2
    assert "Prewalk:" in build_prompts[0]                 # attempt 1 was asked to hand off
    assert "Prewalk:" not in build_prompts[1]              # attempt 2 is not asked to hand off again
    assert "do X" in build_prompts[1] and "do Y" in build_prompts[1]   # the plan carried forward
    assert "Continue directly from there" in build_prompts[1]

    rows = L.load(ws.root, result.task_id)
    handbacks = [r for r in rows if r["schema"] == L.HANDBACK_SCHEMA and r["node_id"] == "build"]
    assert [h["reason"] for h in handbacks] == ["prewalk_handoff", "done"]
    assert handbacks[0]["exit_code"] == 0 and handbacks[0]["failure_kind"] == "none"
    steward = [r for r in rows if r["schema"] == L.STEWARD_SCHEMA]
    assert [s["action"] for s in steward] == ["handoff_cheap"]
    assert steward[0]["target"] != f"{assigned.host.name}/{assigned.model.id}"

    st = L.task_state(rows)
    assert st.nodes["build"].attempts == 2 and st.nodes["build"].done
    assert st.nodes["build"].cost_usd == pytest.approx(0.32) and st.cost_complete


def test_prewalk_is_opt_in_and_does_nothing_when_disabled(state_home, git_workspace):
    ws = make_ws(git_workspace)
    cfg = ws.config.orchestrate
    assert cfg.prewalk is False
    plan = build_route_plan("ship it", _PREWALK_RAW, _hosts("claude"), cfg)

    launches = {"n": 0}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        launches["n"] += 1
        return 0, f"just finished it\n{PREWALK_SENTINEL}", "", _usage()

    result = run_route(ws, plan, cfg, launch=launch)
    # The sentinel is ignored entirely when prewalk is off: one attempt each
    # (build, verify) -- the model's own text cannot trigger a mechanism the
    # config never turned on.
    assert launches["n"] == 2
    by_id = {o.node_id: o for o in result.outcomes}
    assert by_id["build"].status == "ok" and by_id["build"].attempts == 1
    assert by_id["build"].reason == "done"


def test_prewalk_ignores_a_non_mutation_node(state_home, git_workspace):
    from dataclasses import replace

    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, prewalk=True)
    raw = {"nodes": [{"id": "review", "goal": "review the design", "role": "review",
                      "min_tier": "frontier", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude"), cfg)

    launches = {"n": 0}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        launches["n"] += 1
        return 0, f"reviewed\n{PREWALK_SENTINEL}", "", _usage()

    result = run_route(ws, plan, cfg, launch=launch)
    assert launches["n"] == 1   # a review node is never mutation-shaped, prewalk never arms
    assert result.outcomes[0].status == "ok" and result.outcomes[0].reason == "done"


def test_prewalk_keeps_the_isolated_worktree_edit_instead_of_resetting_it(
    state_home, git_workspace
):
    """The edit already landed on disk before the handoff -- unlike a failed
    attempt's retry, that work must survive into the continuation attempt."""
    import subprocess
    from dataclasses import replace

    (git_workspace / "foo.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "foo.py"], cwd=git_workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=git_workspace, check=True)
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, prewalk=True, isolated_worktrees=True)
    raw = {"nodes": [
        {"id": "build", "goal": "add the feature", "role": "implement",
         "min_tier": "frontier", "deps": [], "targets": ["foo.py"]},
        {"id": "verify", "goal": "prove it", "role": "verify",
         "min_tier": "economy", "deps": ["build"]},
    ]}
    plan = build_route_plan("ship it", raw, _hosts("claude"), cfg)
    assigned = plan.assigned[0]
    seen_on_continuation = {}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        if "node \'build\'" in prompt and model == assigned.model.launch_id:
            (root / "foo.py").write_text("new\n", encoding="utf-8")
            return 0, f"planned it\n{PREWALK_SENTINEL}", "", _usage(cost=0.3)
        if "node \'build\'" in prompt:
            seen_on_continuation["foo.py"] = (root / "foo.py").read_text(encoding="utf-8")
        return 0, "done", "", _usage(cost=0.02)

    result = run_route(ws, plan, cfg, launch=launch)
    by_id = {o.node_id: o for o in result.outcomes}
    assert by_id["build"].status == "ok"
    assert seen_on_continuation["foo.py"] == "new\n"   # not reset back to "old"
    assert (git_workspace / "foo.py").read_text(encoding="utf-8") == "new\n"


def test_prewalk_does_not_arm_without_a_second_attempt_or_a_cheaper_model(
    state_home, git_workspace
):
    """Codex review of PR #33: asking a compliant frontier worker to stop
    after one edit when max_attempts is 1, or when no cheaper unattended
    model is installed, turned a task it could have finished into a
    guaranteed stop_blocked. The hint is only added when the handoff it
    asks for can happen."""
    from dataclasses import replace

    ws = make_ws(git_workspace)
    prompts = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        prompts.append(prompt)
        return 0, "done", "", _usage(cost=0.02, turns=3)

    # One attempt allowed: no handoff is possible, so no hint.
    cfg = replace(ws.config.orchestrate, prewalk=True, max_attempts=1)
    plan = build_route_plan("ship it", _PREWALK_RAW, _hosts("claude"), cfg)
    result = run_route(ws, plan, cfg, launch=launch)
    assert {o.status for o in result.outcomes} == {"ok"}
    assert not any("Prewalk:" in p for p in prompts)

    # Two attempts, but only the frontier model installed: nothing to hand to.
    prompts.clear()
    frontier_only = [
        replace(h, spec=replace(h.spec, models=tuple(m for m in h.spec.models if m.tier == "frontier")))
        for h in _hosts("claude")
    ]
    cfg = replace(ws.config.orchestrate, prewalk=True, max_attempts=2)
    plan = build_route_plan("ship it", _PREWALK_RAW, frontier_only, cfg)
    result = run_route(ws, plan, cfg, launch=launch)
    assert {o.status for o in result.outcomes} == {"ok"}
    assert not any("Prewalk:" in p for p in prompts)


def test_prewalk_handoff_is_priced_against_the_budget(state_home, git_workspace):
    """Codex review of PR #33 (P1): the handoff bypassed the steward's menu
    and only checked `remaining <= 0`, so a frontier attempt that spent most
    of an explicit budget handed off into a cheap attempt the ledger knew it
    could not cover. The cheap attempt is priced like any claim."""
    from dataclasses import replace

    ws = make_ws(git_workspace)
    base = replace(ws.config.orchestrate, prewalk=True)
    plan = build_route_plan("ship it", _PREWALK_RAW, _hosts("claude"), base)
    build = plan.assigned[0]
    from ctx.steward import de_escalation_target

    cheap_host, cheap_model = de_escalation_target(build.model, list(plan.hosts))
    cheap_est = cheap_host.model_price(cheap_model.id).cost_usd(
        input_tokens=build.node.est_input_tokens, output_tokens=build.node.est_output_tokens)
    # The first claim must clear the frontier estimate; the frontier attempt
    # then spends exactly that, leaving less than the cheap estimate.
    frontier_cost = float(build.est_cost_usd)
    cfg = replace(base, budget_usd=frontier_cost + cheap_est * 0.5)
    launches = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        launches.append(model)
        if "node \'build\'" in prompt and model == build.model.launch_id:
            return 0, f"plan\n(edited)\n{PREWALK_SENTINEL}", "", _usage(cost=frontier_cost, turns=6)
        return 0, "done", "", _usage(cost=0.02, turns=3)

    result = run_route(ws, plan, cfg, launch=launch)
    by_id = {o.node_id: o for o in result.outcomes}
    assert by_id["build"].status == "failed" and by_id["build"].steward_action == "stop_budget"
    assert launches.count(cheap_model.launch_id) == 0, "the cheap attempt must not launch"
    rows = L.load(ws.root, result.task_id)
    stewards = [r for r in rows if r["schema"] == L.STEWARD_SCHEMA and r["node_id"] == "build"]
    assert [s["action"] for s in stewards] == ["handoff_cheap", "stop_budget"]
    assert L.task_state(rows).spent_usd <= cfg.budget_usd
