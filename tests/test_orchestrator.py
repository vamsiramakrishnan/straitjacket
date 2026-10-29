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
    _launch_host,
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
from ctx.sessiondir import session_reads_path


def _hosts(*installed):
    def which(b):
        return f"/usr/bin/{b}" if b in installed else None

    return [d for d in hosts.detect_all(which=which) if d.installed and d.harnessable]


def _ok_launch(host, root, prompt, exe, *, timeout, model=""):
    return 0, f"{host.name} ok at repo:x.py:1", ""


def test_antigravity_launch_adapts_model_id_and_required_effort(monkeypatch, tmp_path):
    host = next(h for h in _hosts("antigravity") if h.name == "antigravity")
    seen = {}

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Completed()

    monkeypatch.setattr("ctx.orchestrator._run_bounded", fake_run)
    code, out, err, usage = _launch_host(
        host,
        tmp_path,
        "task",
        "ctx",
        timeout=5,
        model="gemini-3.5-flash-lite",
    )
    assert (code, out, err, usage) == (0, "ok", "", None)
    assert seen["argv"][:5] == [
        host.path,
        "--model",
        "gemini-3.5-flash",
        "--effort",
        "low",
    ]


@pytest.mark.parametrize(
    ("binary", "host_name", "model", "stdout", "flag"),
    [
        (
            "claude",
            "claude",
            "haiku",
            json.dumps(
                {
                    "result": "claude done",
                    "total_cost_usd": 0.01,
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }
            ),
            "--output-format",
        ),
        (
            "codex",
            "codex",
            "gpt-5.6-luna",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "codex done",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    ),
                ]
            ),
            "--json",
        ),
        (
            "ctx-agy",
            "antigravity-sdk",
            "gemini-3.6-flash",
            'sdk done\n{"input_tokens": 10, "output_tokens": 2}',
            "--json",
        ),
    ],
)
def test_structured_host_launches_return_clean_text_and_actual_usage(
    monkeypatch, tmp_path, binary, host_name, model, stdout, flag
):
    host = next(h for h in _hosts(binary) if h.name == host_name)
    seen = {}

    class Completed:
        returncode = 0
        stderr = ""

    Completed.stdout = stdout

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Completed()

    monkeypatch.setattr("ctx.orchestrator._run_bounded", fake_run)
    code, out, err, usage = _launch_host(
        host, tmp_path, "task", "ctx", timeout=5, model=model
    )
    assert code == 0 and err == ""
    assert out in {"claude done", "codex done", "sdk done"}
    assert usage is not None and usage.total_tokens == 12
    assert flag in seen["argv"]


def test_coordinator_pin_cannot_bypass_unattended_host_gate():
    hosts_ = _hosts("claude", "codex", "antigravity")
    automatic = fallback_route("Run the named pytest test", hosts_, _POLICY())
    assert automatic.assigned[0].host.name != "antigravity"

    pinned = build_route_plan(
        "Run the named pytest test",
        {
            "nodes": [
                {
                    "id": "verify",
                    "role": "verify",
                    "min_tier": "economy",
                    "needs": ["verify"],
                    "deps": [],
                    "host": "antigravity",
                }
            ]
        },
        hosts_,
        _POLICY(),
    )
    assert pinned.assigned[0].host.name != "antigravity"


def test_explicitly_approved_interactive_pin_is_honored():
    pinned = build_route_plan(
        "Run the named pytest test",
        {
            "nodes": [
                {
                    "id": "verify",
                    "role": "verify",
                    "min_tier": "economy",
                    "needs": ["verify"],
                    "deps": [],
                    "host": "antigravity",
                }
            ]
        },
        _hosts("claude", "codex", "antigravity"),
        _POLICY(),
        allow_interactive_pins=True,
    )
    assert pinned.assigned[0].host.name == "antigravity"


# --------------------------------------------------------------- fallback route


def test_fallback_route_uses_unattended_models_and_flagship_planner():
    plan = fallback_route("t", _hosts("claude", "antigravity"), _POLICY())
    by_id = {a.node.id: a for a in plan.assigned}
    # plan takes the frontier FLAGSHIP (Opus), not the cheapest frontier model.
    assert by_id["plan"].node.prefer == "strong"
    assert by_id["plan"].host.name == "claude" and by_id["plan"].model.id == "claude-opus-4.8"
    # Interactive-only Antigravity is excluded from automatic execution.
    assert by_id["implement"].model.id == "claude-sonnet-4.6"
    assert by_id["explore"].model.tier == "economy"
    assert by_id["verify"].model.tier == "economy"


def test_economy_tier_does_not_undercut_required_implementation_capability():
    from dataclasses import replace

    hosts = _hosts("claude", "codex", "antigravity")
    complex_ = fallback_route("t", hosts, _POLICY())
    simple = fallback_route("t", hosts, replace(_POLICY(), implement_tier="economy"))
    impl = lambda p: next(a for a in p.assigned if a.node.id == "implement")  # noqa: E731
    # No unattended economy model declares implement/edit. Coverage is primary,
    # so both routes select the cheapest capable standard model instead of a
    # cheaper model that cannot complete the node.
    assert impl(complex_).model.id == "gpt-5.6-terra"
    assert impl(simple).model.id == "gpt-5.6-terra"


def test_fallback_route_beats_single_premium():
    plan = fallback_route("t", _hosts("claude", "antigravity"), _POLICY())
    assert plan.est_total_usd < plan.est_single_premium_usd


def test_single_host_routes_across_its_own_models():
    # Even one harness collaborates *across its models*: cheap explore/verify,
    # frontier plan, standard implement — so it still beats an all-frontier run.
    plan = fallback_route("t", _hosts("claude"), _POLICY())
    assert {a.host.name for a in plan.assigned} == {"claude"}
    tiers = {a.node.id: a.model.tier for a in plan.assigned}
    assert tiers["explore"] == "economy" and tiers["plan"] == "frontier"
    assert plan.est_total_usd < plan.est_single_premium_usd


@pytest.mark.parametrize(
    ("task", "node_ids", "tiers"),
    [
        ("Run the named pytest test tests/test_cli.py::test_help", ["verify"], ["economy"]),
        ("Review the current diff", ["review"], ["standard"]),
        ("Explain the following code", ["answer"], ["standard"]),
        ("Inspect the named function", ["answer"], ["standard"]),
        ("Fix a one-line typo in README.md", ["implement", "verify"], ["economy", "economy"]),
        (
            "Implement normalize_whitespace in sample/__init__.py. It must collapse whitespace; add pytest tests",
            ["explore", "implement", "verify"],
            ["economy", "standard", "economy"],
        ),
    ],
)
def test_fallback_route_uses_smallest_completing_fast_path(task, node_ids, tiers):
    plan = fallback_route(task, _hosts("claude", "codex", "antigravity"), _POLICY())
    assert [assigned.node.id for assigned in plan.assigned] == node_ids
    assert [assigned.node.min_tier for assigned in plan.assigned] == tiers


def test_bounded_feature_uses_live_proven_implementer():
    task = (
        "Implement normalize_whitespace in sample/__init__.py. It must collapse "
        "whitespace; add pytest tests"
    )
    plan = fallback_route(task, _hosts("claude", "codex", "antigravity"), _POLICY())
    implement = next(node for node in plan.assigned if node.node.id == "implement")
    assert implement.host.name == "claude"
    assert implement.model.id == "claude-sonnet-4.6"


@pytest.mark.parametrize(
    "task",
    [
        "Implement normalize_whitespace in sample/__init__.py and add pytest tests",
        "Implement authorization in sample/__init__.py. It must be secure; add pytest tests",
    ],
)
def test_bounded_feature_keeps_full_route_when_contract_or_risk_gate_fails(task):
    plan = fallback_route(task, _hosts("claude", "codex", "antigravity"), _POLICY())
    assert [assigned.node.id for assigned in plan.assigned] == [
        "explore",
        "plan",
        "implement",
        "verify",
    ]


def test_fallback_fast_path_costs_less_than_general_route():
    hosts_ = _hosts("claude", "codex", "antigravity")
    general = fallback_route("Migrate the authorization architecture", hosts_, _POLICY())
    simple = fallback_route("Fix a typo in README.md", hosts_, _POLICY())
    assert len(general.assigned) == 4
    assert len(simple.assigned) == 2
    assert simple.est_total_usd < general.est_total_usd


@pytest.mark.parametrize(
    "task",
    [
        "Summarize the latest release from the supplied context",
        "Summarize the customer testimony",
    ],
)
def test_fallback_fast_path_does_not_substring_match_test(task):
    plan = fallback_route(task, _hosts("claude", "codex", "antigravity"), _POLICY())
    assert [assigned.node.id for assigned in plan.assigned] == ["answer"]


def test_route_execution_appends_privacy_safe_receipt(state_home, git_workspace):
    ws = make_ws(git_workspace)
    secret_task = "Fix a typo in README.md secret-customer-text"
    plan = fallback_route(secret_task, _hosts("claude", "antigravity"), _POLICY())
    result = run_route(ws, plan, _POLICY(), launch=_ok_launch)

    ledger = session_reads_path(ws.root, "route.jsonl")
    receipt = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert receipt["schema"] == "ctx.route-run/v1"
    assert receipt["task_profile"]["kind"] == "simple_edit"
    assert receipt["task_profile"]["named_target"] is True
    assert receipt["task_profile"]["named_acceptance"] is False
    assert receipt["task_profile"]["high_risk_scope"] is False
    assert receipt["route"]["source"] == "deterministic_fast"
    assert [node["id"] for node in receipt["route"]["nodes"]] == [
        "implement",
        "verify",
    ]
    assert receipt["measurement"]["route_completed"] is True
    assert receipt["measurement"]["task_success"] == "unmeasured"
    assert receipt["measurement"]["verification_passed"] is True
    assert receipt["measurement"]["estimated_spend_usd"] == pytest.approx(
        result.estimated_spend_usd
    )
    assert receipt["measurement"]["actual_usage"]["status"] == "unavailable"
    assert receipt["measurement"]["actual_usage"]["attempts_total"] == 2
    assert secret_task not in ledger.read_text(encoding="utf-8")


def test_route_aggregates_actual_usage_without_hiding_missing_attempts(
    state_home, git_workspace
):
    ws = make_ws(git_workspace)
    plan = fallback_route(
        "Fix a one-line typo in README.md", _hosts("claude"), _POLICY()
    )
    calls = 0

    def launch(host, root, prompt, exe, *, timeout, model=""):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0, "implemented", "", {
                "input_tokens": 100,
                "cache_read_tokens": 50,
                "output_tokens": 25,
                "cost_usd": 0.004,
                "cost_basis": "priced_tokens",
                "source": "test_json",
            }
        return 0, "verified", ""

    result = run_route(ws, plan, _POLICY(), launch=launch)
    assert result.actual_usage == {
        "status": "partial",
        "attempts_total": 2,
        "attempts_measured": 1,
        "input_tokens": 100,
        "cache_read_tokens": 50,
        "cache_write_tokens": 0,
        "output_tokens": 25,
        "total_tokens": 175,
        "cost_usd": pytest.approx(0.004),
        "cost_complete": False,
        "cost_basis": "priced_tokens",
        "sources": ["test_json"],
        "turns": 0,
        "turns_reported": 0,
    }

    receipt = json.loads(
        session_reads_path(ws.root, "route.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert receipt["measurement"]["actual_usage"]["status"] == "partial"
    assert receipt["outcomes"][0]["actual_usage"]["status"] == "available"
    assert receipt["outcomes"][1]["actual_usage"]["status"] == "unavailable"


def test_route_counts_failed_and_escalated_attempt_usage(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {
        "nodes": [
            {"id": "a", "goal": "x", "min_tier": "economy", "deps": []}
        ]
    }
    plan = build_route_plan("t", raw, _hosts("claude"), _POLICY())
    calls = 0

    def launch(host, root, prompt, exe, *, timeout, model=""):
        nonlocal calls
        calls += 1
        usage = {
            "input_tokens": 10 * calls,
            "output_tokens": calls,
            "cost_usd": 0.001 * calls,
            "source": "test_json",
        }
        return ((1, "", "failed", usage) if calls == 1 else (0, "ok", "", usage))

    result = run_route(ws, plan, _POLICY(), launch=launch)
    assert result.outcomes[0].escalated_to
    assert result.actual_usage["attempts_total"] == 2
    assert result.actual_usage["attempts_measured"] == 2
    assert result.actual_usage["total_tokens"] == 33
    assert result.actual_usage["cost_usd"] == pytest.approx(0.003)


# --------------------------------------------------------------- route IR


def _plan_raw(**over):
    base = {
        "nodes": [
            {"id": "a", "goal": "x", "min_tier": "economy", "needs": ["search"], "deps": []},
            {"id": "b", "goal": "y", "min_tier": "frontier", "needs": ["edit"], "deps": ["a"]},
            {"id": "c", "goal": "check y", "role": "verify", "min_tier": "economy", "needs": ["verify", "test"], "deps": ["b"]},
        ]
    }
    base.update(over)
    return base


def test_build_route_plan_assigns_by_model_tier():
    plan = build_route_plan("t", _plan_raw(), _hosts("claude", "antigravity"), _POLICY())
    by_id = {a.node.id: a for a in plan.assigned}
    assert by_id["a"].model.tier == "economy"    # search node
    assert by_id["b"].model.tier == "frontier"   # edit-at-frontier node


def test_host_pin_is_honored_when_installed():
    raw = {"nodes": [{"id": "a", "goal": "x", "min_tier": "economy", "host": "claude", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude", "antigravity"), _POLICY())
    assert plan.assigned[0].host.name == "claude"


def test_model_pin_is_honored():
    # The coordinator can pin a specific model (e.g. Opus for planning quality),
    # overriding the cheapest-model default.
    raw = {"nodes": [{"id": "p", "goal": "plan it", "min_tier": "frontier",
                      "host": "claude", "model": "claude-opus-4.8", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude", "antigravity"), _POLICY())
    assert plan.assigned[0].host.name == "claude"
    assert plan.assigned[0].model.id == "claude-opus-4.8"


def test_coordinator_mutation_requires_a_downstream_verifier():
    raw = {
        "nodes": [
            {
                "id": "implement",
                "role": "implement",
                "min_tier": "standard",
                "needs": ["implement", "edit", "test"],
                "deps": [],
            }
        ]
    }
    with pytest.raises(RouteError, match="no downstream verification"):
        build_route_plan("add feature", raw, _hosts("claude", "codex"), _POLICY())


def test_coordinator_mutation_accepts_dependent_verifier():
    raw = {
        "nodes": [
            {
                "id": "implement",
                "role": "implement",
                "min_tier": "standard",
                "needs": ["implement", "edit"],
                "deps": [],
            },
            {
                "id": "verify",
                "role": "verify",
                "min_tier": "economy",
                "needs": ["verify", "test"],
                "deps": ["implement"],
            },
        ]
    }
    plan = build_route_plan("add feature", raw, _hosts("claude", "codex"), _POLICY())
    assert [assigned.node.id for assigned in plan.assigned] == ["implement", "verify"]


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
        # record whether each node saw an upstream checkpoint, keyed by node id
        nid = "a" if "node 'a'" in prompt else "b"
        saw[nid] = "checkpoint:" in prompt
        return 0, "ok", ""

    run_route(ws, plan, ws.config.orchestrate, launch=launch)
    assert saw["a"] is False   # node a, no upstream
    assert saw["b"] is True    # node b sees a's checkpoint


def test_failed_node_escalates_to_stronger_model(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [{"id": "a", "goal": "x", "min_tier": "economy", "deps": []}]}
    plan = build_route_plan("t", raw, _hosts("claude", "antigravity"), ws.config.orchestrate)
    # Economy models fail; a stronger tier recovers.
    def launch(host, root, prompt, exe, *, timeout, model=""):
        return (1, "", "boom") if model.endswith("flash-lite") or "haiku" in model else (0, "recovered", "")

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    o = result.outcomes[0]
    assert o.status == "ok"
    assert o.escalated_to and "/" in o.escalated_to   # escalated to a stronger (host, model)


@pytest.mark.parametrize(
    "false_success",
    [
        "jetski: no output produced; permission auto-denied",
        "Blocked by the read-only workspace: no source or test files could be modified.",
        "VERIFICATION RESULT: Implementation incomplete. The task is **NOT COMPLETE**.",
    ],
)
def test_zero_exit_explicit_failure_escalates_instead_of_counting_as_ok(
    state_home, git_workspace, false_success
):
    ws = make_ws(git_workspace)
    raw = {
        "nodes": [
            {"id": "a", "goal": "x", "min_tier": "economy", "deps": []}
        ]
    }
    plan = build_route_plan("t", raw, _hosts("claude", "codex"), ws.config.orchestrate)
    calls = 0

    def launch(host, root, prompt, exe, *, timeout, model=""):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0, false_success, ""
        return 0, "recovered with a real result", ""

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    assert calls == 2
    assert result.outcomes[0].status == "ok"
    assert result.outcomes[0].escalated_to


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


def test_orchestrate_rejects_only_interactive_host(
    state_home, git_workspace, monkeypatch
):
    ws = make_ws(git_workspace)
    monkeypatch.setattr(
        "ctx.orchestrator.installed_harnessable",
        lambda **kw: _hosts("antigravity"),
    )
    code, text = orchestrate(ws, "Run the named pytest test", dry_run=True)
    assert code == 1
    assert "no installed host can run unattended" in text


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


def test_orchestrate_fast_path_skips_coordinator(state_home, git_workspace, monkeypatch):
    ws = make_ws(git_workspace)
    monkeypatch.setattr(
        "ctx.orchestrator.installed_harnessable",
        lambda **kw: _hosts("claude", "antigravity"),
    )

    def unexpected_coordinator(*args, **kwargs):
        raise AssertionError("simple task must not spend a coordinator turn")

    monkeypatch.setattr("ctx.orchestrator.invoke_coordinator", unexpected_coordinator)
    code, text = orchestrate(ws, "Run the named pytest test", dry_run=True)
    assert code == 0
    assert "routing (1 nodes, 1 waves)" in text
    assert "coordinator skipped" in text


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
