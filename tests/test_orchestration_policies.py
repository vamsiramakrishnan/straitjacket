from __future__ import annotations

import threading
import time

from conftest import make_ws
from ctx import hosts
from ctx.config import OrchestratePolicy
from ctx.handoff_policy import choose_handoff
from ctx.mutation_policy import choose_mutation_isolation
from ctx.orchestrator import (
    RouteNode,
    _bounded_handoff_state,
    build_route_plan,
    run_route,
)
from ctx.verification_policy import choose_verification
from ctx.wave_policy import choose_wave


def _hosts(*installed):
    def which(binary):
        return f"/usr/bin/{binary}" if binary in installed else None

    return [
        host
        for host in hosts.detect_all(which=which)
        if host.installed and host.harnessable
    ]


def _tracking_launch():
    lock = threading.Lock()
    state = {"active": 0, "maximum": 0}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1
        return 0, f"{host.name} completed at repo:result.txt:1", ""

    return state, launch


def test_policy_functions_keep_hard_boundaries():
    assert choose_wave({"ready_count": 4, "readonly_count": 4}) == "parallel_four"
    assert choose_wave(
        {
            "ready_count": 4,
            "readonly_count": 4,
            "provider_rate_limited": True,
        }
    ) == "serial"
    assert choose_wave(
        {"ready_count": 3, "readonly_count": 2, "mutation_count": 1}
    ) == "readonly_first"
    assert choose_mutation_isolation(
        {"mutation_count": 2, "shared_workspace": True}
    ) == "serial_workspace"
    assert choose_handoff({"failed": True}) == "expanded"
    assert choose_verification(
        {"mutation": True, "complexity": 4, "alternate_host": True}
    ) == "independent_economy"


def test_readonly_frontier_runs_in_parallel(tmp_path):
    ws = make_ws(tmp_path)
    available = _hosts("claude", "codex")
    raw = {
        "nodes": [
            {"id": "left", "role": "explore", "needs": ["search"]},
            {"id": "right", "role": "explore", "needs": ["search"]},
            {
                "id": "answer",
                "role": "answer",
                "needs": ["summarize"],
                "deps": ["left", "right"],
            },
        ]
    }
    plan = build_route_plan("explain the two modules", raw, available, ws.config.orchestrate)
    concurrency, launch = _tracking_launch()
    result = run_route(ws, plan, ws.config.orchestrate, launch=launch, max_workers=4)
    assert concurrency["maximum"] >= 2
    assert result.wave_policies[0].startswith("parallel_two/")
    assert all(outcome.status == "ok" for outcome in result.outcomes)


def test_shared_workspace_mutations_are_serialized(tmp_path):
    ws = make_ws(tmp_path)
    available = _hosts("claude", "codex")
    raw = {
        "nodes": [
            {"id": "edit_a", "role": "implement", "needs": ["edit"]},
            {"id": "edit_b", "role": "implement", "needs": ["edit"]},
            {
                "id": "verify",
                "role": "verify",
                "needs": ["verify", "test"],
                "deps": ["edit_a", "edit_b"],
            },
        ]
    }
    plan = build_route_plan("change two explicitly independent files", raw, available, ws.config.orchestrate)
    concurrency, launch = _tracking_launch()
    result = run_route(ws, plan, ws.config.orchestrate, launch=launch, max_workers=4)
    assert concurrency["maximum"] == 1
    assert result.wave_policies[0] == "mutation_serial/serial_workspace"
    assert all(outcome.status == "ok" for outcome in result.outcomes)


def test_high_risk_verifier_uses_an_independent_host_when_available():
    available = _hosts("ctx-agy", "claude", "codex")
    raw = {
        "nodes": [
            {
                "id": "implement",
                "role": "implement",
                "min_tier": "standard",
                "needs": ["implement", "edit", "code"],
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
    plan = build_route_plan(
        "implement an authorization security change",
        raw,
        available,
        OrchestratePolicy(),
    )
    by_id = {item.node.id: item for item in plan.assigned}
    assert by_id["verify"].host.name != by_id["implement"].host.name
    assert by_id["verify"].verification_policy == "independent_standard"


def test_handoff_budget_keeps_head_tail_and_exact_address_hint():
    node = RouteNode("explore", "inspect", "explore", "economy", (), (), 10, 10)
    text = "root cause\n" + ("noise\n" * 500) + "verification passed"
    compact = _bounded_handoff_state(node, text, "compact")
    assert len(compact) <= 600
    assert compact.startswith("root cause")
    assert compact.endswith("verification passed")
    assert "resolve blob:" in compact
    assert "checkpoint evidence address" in _bounded_handoff_state(
        node, text, "address_only"
    )


def test_massive_orchestration_policy_matrix_is_a_promotion_gate():
    from evals.alphaevolve.orchestration_matrix import run_matrix

    result = run_matrix()
    assert result["cases"] >= 250_000
    assert result["failures"] == 0
    assert result["all_gates_pass"] is True
