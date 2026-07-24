"""Acceptance: the harness collaboration orchestrator.

Planning/pricing is pure and asserted deterministically. Execution is exercised
with an injected launcher so no real CLI runs, verifying the CAS checkpoint
handoff (each phase reads the prior phase's checkpoint) and fail-open behaviour.
"""

from __future__ import annotations

from ctx import hosts
from ctx.config import OrchestratePolicy
from ctx.orchestrator import (
    cost_ladder,
    orchestrate,
    plan_orchestration,
    render_plan,
    run_orchestration,
)

from conftest import make_ws


def _hosts(*installed):
    def which(b):
        return f"/usr/bin/{b}" if b in installed else None

    return [d for d in hosts.detect_all(which=which) if d.installed and d.harnessable]


def test_cost_ladder_cheapest_first():
    ladder = cost_ladder(_hosts("claude", "codex", "antigravity"))
    # antigravity (gemini-flash, $3/Mout) is cheapest; claude/codex tie on
    # output ($15) and break on input (codex 2.5 < claude 3.0).
    assert [d.name for d in ladder] == ["antigravity", "codex", "claude"]


def test_lean_routes_cheapest_capable_routes_premium():
    plan = plan_orchestration("t", _hosts("claude", "antigravity"), OrchestratePolicy())
    by_name = {a.phase.name: a.host.name for a in plan.assignments}
    assert by_name["explore"] == "antigravity"   # lean -> cheapest
    assert by_name["implement"] == "claude"      # capable -> premium
    assert by_name["review"] == "antigravity"


def test_config_pin_overrides_cost_pick():
    cfg = OrchestratePolicy(lean_host="claude", capable_host="antigravity")
    plan = plan_orchestration("t", _hosts("claude", "antigravity"), cfg)
    by_name = {a.phase.name: a.host.name for a in plan.assignments}
    assert by_name["explore"] == "claude"        # pinned lean
    assert by_name["implement"] == "antigravity" # pinned capable


def test_single_host_routes_everything_to_it():
    plan = plan_orchestration("t", _hosts("claude"), OrchestratePolicy())
    assert {a.host.name for a in plan.assignments} == {"claude"}


def test_plan_prices_and_beats_single_premium_baseline():
    plan = plan_orchestration("t", _hosts("claude", "antigravity"), OrchestratePolicy())
    # Collaboration total is the sum of per-phase costs...
    assert abs(plan.est_total_usd - sum(a.est_cost_usd for a in plan.assignments)) < 1e-9
    # ...and cheaper than running every phase on the premium harness, because
    # two of three phases went to the economy harness.
    assert plan.est_total_usd < plan.est_single_premium_usd


def test_render_plan_is_deterministic():
    plan = plan_orchestration("fix x", _hosts("claude", "codex"), OrchestratePolicy())
    assert render_plan(plan) == render_plan(plan)
    assert 'task: "fix x"' in render_plan(plan)


def test_no_installed_host_raises():
    import pytest

    with pytest.raises(ValueError):
        plan_orchestration("t", [], OrchestratePolicy())


# --------------------------------------------------------------- execution


def test_run_threads_checkpoints_between_phases(state_home, git_workspace):
    ws = make_ws(git_workspace)
    plan = plan_orchestration("add retry", _hosts("claude", "antigravity"), ws.config.orchestrate)

    saw_prior = []

    def launch(host, ws_root, prompt, exe, *, timeout):
        saw_prior.append("checkpoint:" in prompt)
        return 0, f"{host.name}: found it at repo:client.py:42", ""

    result = run_orchestration(ws, plan, launch=launch)
    assert [o.status for o in result.outcomes] == ["ok", "ok", "ok"]
    # Every phase minted a checkpoint handle...
    assert all(o.checkpoint_ref and o.checkpoint_ref.startswith("checkpoint:")
               for o in result.outcomes)
    # ...and only the first phase ran without a prior checkpoint in its prompt.
    assert saw_prior == [False, True, True]


def test_run_is_fail_open_on_launcher_error(state_home, git_workspace):
    ws = make_ws(git_workspace)
    plan = plan_orchestration("t", _hosts("claude", "antigravity"), ws.config.orchestrate)

    def boom(host, ws_root, prompt, exe, *, timeout):
        return 127, "", "OSError: no such binary"

    result = run_orchestration(ws, plan, launch=boom)
    # A non-zero exit is recorded as failed, never raised; the run completes.
    assert [o.status for o in result.outcomes] == ["failed", "failed", "failed"]


def test_orchestrate_dry_run_prices_without_running(state_home, git_workspace, monkeypatch):
    ws = make_ws(git_workspace)
    monkeypatch.setattr(
        "ctx.orchestrator.installed_harnessable",
        lambda **kw: _hosts("claude", "antigravity"),
    )
    code, text = orchestrate(ws, "do a thing", dry_run=True)
    assert code == 0
    assert "dry run" in text
    assert "estimated total" in text
    assert "run complete" not in text  # never executed


def test_orchestrate_confirm_gate_stops_before_running(state_home, git_workspace, monkeypatch):
    # confirm is driven from committed ctx.toml, the real config path.
    (git_workspace / "ctx.toml").write_text(
        "version = 1\n[orchestrate]\nconfirm = true\n", encoding="utf-8"
    )
    ws = make_ws(git_workspace)
    assert ws.config.orchestrate.confirm is True
    monkeypatch.setattr(
        "ctx.orchestrator.installed_harnessable",
        lambda **kw: _hosts("claude"),
    )
    code, text = orchestrate(ws, "task", force_run=False)
    assert code == 0 and "confirm=true" in text
    # --run overrides the gate (would execute; launcher missing -> failed, ok).
    code2, text2 = orchestrate(ws, "task", force_run=True)
    assert "run complete" in text2
