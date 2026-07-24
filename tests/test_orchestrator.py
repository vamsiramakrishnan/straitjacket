"""Acceptance: the harness collaboration orchestrator (closed-loop, capability×price).

Route IR, validation, pricing, and the wave scheduler are pure/deterministic.
Coordinator invocation and node execution are injected so the closed loop —
parallel waves, checkpoint handoff, failure escalation, bounded re-planning — is
exercised without a live CLI.
"""

from __future__ import annotations

import json

import pytest

from ctx import hosts
from ctx.orchestrator import (
    RouteError,
    _extract_json,
    build_menu,
    build_route_plan,
    fallback_route,
    invoke_coordinator,
    orchestrate,
    render_route_plan,
    run_route,
)

from conftest import make_ws


def _hosts(*installed):
    def which(b):
        return f"/usr/bin/{b}" if b in installed else None

    return [d for d in hosts.detect_all(which=which) if d.installed and d.harnessable]


def _ok_launch(host, root, prompt, exe, *, timeout, model=""):
    return 0, f"{host.name} ok at repo:x.py:1", ""


# --------------------------------------------------------------- fallback route


def test_fallback_route_is_capability_routed():
    plan = fallback_route("t", _hosts("claude", "antigravity"), _POLICY())
    by_id = {a.node.id: a.host.name for a in plan.assigned}
    assert by_id["explore"] == "antigravity"    # economy tier
    assert by_id["implement"] == "claude"        # frontier tier
    assert by_id["verify"] == "antigravity"      # economy tier
    # Coordinator is the cheapest planner (antigravity/flash-lite).
    assert plan.coordinator.name == "antigravity"


def test_fallback_route_beats_single_premium():
    plan = fallback_route("t", _hosts("claude", "antigravity"), _POLICY())
    assert plan.est_total_usd < plan.est_single_premium_usd


def test_single_host_degrades_with_zero_saving():
    plan = fallback_route("t", _hosts("claude"), _POLICY())
    assert {a.host.name for a in plan.assigned} == {"claude"}
    assert plan.est_total_usd == pytest.approx(plan.est_single_premium_usd)


# --------------------------------------------------------------- route IR


def _plan_raw(**over):
    base = {
        "nodes": [
            {"id": "a", "goal": "x", "min_tier": "economy", "needs": ["search"], "deps": []},
            {"id": "b", "goal": "y", "min_tier": "frontier", "needs": ["edit"], "deps": ["a"]},
        ]
    }
    base.update(over)
    return base


def test_build_route_plan_assigns_by_capability():
    plan = build_route_plan("t", _plan_raw(), _hosts("claude", "antigravity"), _POLICY())
    by_id = {a.node.id: a.host.name for a in plan.assigned}
    assert by_id["a"] == "antigravity"   # economy
    assert by_id["b"] == "claude"        # frontier


def test_host_pin_is_honored_when_installed():
    raw = {"nodes": [{"id": "a", "goal": "x", "min_tier": "economy", "host": "claude", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude", "antigravity"), _POLICY())
    assert plan.assigned[0].host.name == "claude"


def test_cycle_is_rejected():
    raw = {"nodes": [
        {"id": "a", "goal": "x", "deps": ["b"]},
        {"id": "b", "goal": "y", "deps": ["a"]},
    ]}
    with pytest.raises(RouteError):
        build_route_plan("t", raw, _hosts("claude"), _POLICY())


def test_unknown_dep_is_rejected():
    raw = {"nodes": [{"id": "a", "goal": "x", "deps": ["ghost"]}]}
    with pytest.raises(RouteError):
        build_route_plan("t", raw, _hosts("claude"), _POLICY())


def test_over_budget_is_rejected():
    from dataclasses import replace

    cfg = replace(_POLICY(), budget_usd=0.001)
    with pytest.raises(RouteError):
        build_route_plan("t", _plan_raw(), _hosts("claude", "antigravity"), cfg)


def test_node_cap_truncates():
    from dataclasses import replace

    cfg = replace(_POLICY(), max_nodes=1)
    raw = {"nodes": [{"id": f"n{i}", "goal": "x", "deps": []} for i in range(5)]}
    plan = build_route_plan("t", raw, _hosts("claude"), cfg)
    assert len(plan.assigned) == 1


def test_waves_are_topological():
    raw = {"nodes": [
        {"id": "a", "goal": "x", "deps": []},
        {"id": "b", "goal": "y", "deps": []},
        {"id": "c", "goal": "z", "deps": ["a", "b"]},
    ]}
    plan = build_route_plan("t", raw, _hosts("claude"), _POLICY())
    waves = plan.waves()
    assert {n.node.id for n in waves[0]} == {"a", "b"}   # independent -> same wave
    assert [n.node.id for n in waves[1]] == ["c"]        # dependent -> next wave


def test_render_route_plan_is_deterministic():
    plan = build_route_plan("fix x", _plan_raw(), _hosts("claude", "antigravity"), _POLICY())
    assert render_route_plan(plan) == render_route_plan(plan)
    assert 'task: "fix x"' in render_route_plan(plan)


def test_menu_lists_installed_by_price():
    menu = build_menu(_hosts("claude", "codex", "antigravity"))
    # cheapest first
    assert menu.index("antigravity") < menu.index("codex") < menu.index("claude")


# --------------------------------------------------------------- coordinator parse


def test_extract_json_tolerates_fences_and_prose():
    text = "here is the plan:\n```json\n{\"schema\":\"ctx.route/v1\",\"nodes\":[]}\n```\nthanks"
    got = _extract_json(text)
    assert got == {"schema": "ctx.route/v1", "nodes": []}


def test_invoke_coordinator_parses_launch_output(state_home, git_workspace):
    ws = make_ws(git_workspace)
    plan_json = json.dumps({"schema": "ctx.route/v1", "nodes": [
        {"id": "a", "goal": "x", "min_tier": "economy", "deps": []}]})

    def launch(host, root, prompt, exe, *, timeout, model=""):
        # the coordinator is the cheap host, pinned to its coord model
        assert model == host.spec.coord_model
        return 0, f"reasoning...\n{plan_json}", ""

    raw = invoke_coordinator(ws, "t", _hosts("claude", "antigravity"), ws.config.orchestrate,
                             exe="/usr/bin/ctx", launch=launch)
    assert raw["nodes"][0]["id"] == "a"


# --------------------------------------------------------------- closed loop


def test_run_route_parallel_wave_all_checkpointed(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [
        {"id": "a", "goal": "x", "min_tier": "economy", "deps": []},
        {"id": "b", "goal": "y", "min_tier": "economy", "deps": []},
        {"id": "c", "goal": "z", "min_tier": "frontier", "deps": ["a", "b"]},
    ]}
    plan = build_route_plan("t", raw, _hosts("claude", "antigravity"), ws.config.orchestrate)
    result = run_route(ws, plan, ws.config.orchestrate, launch=_ok_launch)
    assert [o.status for o in result.outcomes] == ["ok", "ok", "ok"]
    # Every completed node minted a checkpoint even when two ran in parallel.
    assert all(o.checkpoint_ref for o in result.outcomes)


def test_dependent_node_sees_upstream_checkpoints(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [
        {"id": "a", "goal": "x", "min_tier": "economy", "deps": []},
        {"id": "b", "goal": "y", "min_tier": "frontier", "deps": ["a"]},
    ]}
    plan = build_route_plan("t", raw, _hosts("claude", "antigravity"), ws.config.orchestrate)
    saw = {}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        saw[host.name] = "checkpoint:" in prompt
        return 0, f"{host.name} ok", ""

    run_route(ws, plan, ws.config.orchestrate, launch=launch)
    assert saw["antigravity"] is False   # node a, no upstream
    assert saw["claude"] is True         # node b sees a's checkpoint


def test_failed_node_escalates_to_stronger_harness(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [{"id": "a", "goal": "x", "min_tier": "economy", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude", "antigravity"), ws.config.orchestrate)

    def launch(host, root, prompt, exe, *, timeout, model=""):
        return (1, "", "boom") if host.name == "antigravity" else (0, "recovered", "")

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    o = result.outcomes[0]
    assert o.status == "ok" and o.escalated_to == "claude"


def test_dependent_skipped_when_upstream_fails(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [
        {"id": "a", "goal": "x", "min_tier": "frontier", "deps": []},
        {"id": "b", "goal": "y", "min_tier": "frontier", "deps": ["a"]},
    ]}
    plan = build_route_plan("t", raw, _hosts("claude"), ws.config.orchestrate)

    def launch(host, root, prompt, exe, *, timeout, model=""):
        return 1, "", "fail"   # frontier fails, nothing stronger to escalate to

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    by_id = {o.node_id: o.status for o in result.outcomes}
    assert by_id["a"] == "failed" and by_id["b"] == "skipped"


def test_bounded_replan_adds_recovery_node(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [{"id": "main", "goal": "x", "min_tier": "frontier", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude"), ws.config.orchestrate)

    def launch(host, root, prompt, exe, *, timeout, model=""):
        return (1, "", "fail") if "main" in prompt else (0, "recovery ok", "")

    calls = {"n": 0}

    def coordinate(extra):
        calls["n"] += 1
        if calls["n"] > 1:
            return None
        return {"nodes": [{"id": "recover", "goal": "retry", "min_tier": "frontier", "deps": []}]}

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch, coordinate=coordinate)
    by_id = {o.node_id: o.status for o in result.outcomes}
    assert by_id["main"] == "failed"
    assert by_id["recover"] == "ok"
    assert result.replans == 1


# --------------------------------------------------------------- top-level


def test_orchestrate_no_host_errors(state_home, git_workspace, monkeypatch):
    ws = make_ws(git_workspace)
    monkeypatch.setattr("ctx.orchestrator.installed_harnessable", lambda **kw: [])
    code, text = orchestrate(ws, "t")
    assert code == 1 and "no installed harnessable" in text


def test_orchestrate_dry_run_uses_fallback_and_does_not_execute(state_home, git_workspace, monkeypatch):
    ws = make_ws(git_workspace)
    monkeypatch.setattr("ctx.orchestrator.installed_harnessable",
                        lambda **kw: _hosts("claude", "antigravity"))
    # No coordinator output -> deterministic fallback route.
    monkeypatch.setattr("ctx.orchestrator.invoke_coordinator", lambda *a, **k: None)
    code, text = orchestrate(ws, "do a thing", dry_run=True)
    assert code == 0
    assert "dry run" in text and "estimated total" in text
    assert "run complete" not in text


def test_orchestrate_confirm_gate(state_home, git_workspace, monkeypatch):
    (git_workspace / "ctx.toml").write_text(
        "version = 1\n[orchestrate]\nconfirm = true\n", encoding="utf-8")
    ws = make_ws(git_workspace)
    assert ws.config.orchestrate.confirm is True
    monkeypatch.setattr("ctx.orchestrator.installed_harnessable",
                        lambda **kw: _hosts("claude", "antigravity"))
    monkeypatch.setattr("ctx.orchestrator.invoke_coordinator", lambda *a, **k: None)
    code, text = orchestrate(ws, "t", force_run=False)
    assert code == 0 and "confirm=true" in text


def _POLICY():
    from ctx.config import OrchestratePolicy

    return OrchestratePolicy()
