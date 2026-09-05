"""The improvement route, model-free: its plan validates under the router's
own rules, and its gate is arithmetic that fails closed."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import improve_route as ir  # noqa: E402

from ctx import hosts  # noqa: E402


def _roster():
    return [d for d in hosts.detect_all(which=lambda b: f"/usr/bin/{b}" if b == "claude" else None)
            if d.installed and d.harnessable]


def test_route_validates_and_prices_four_nodes(state_home, git_workspace):
    from ctx.orchestrator import build_route_plan
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(git_workspace))
    plan = build_route_plan("improve src/ctx", ir.raw_route("src/ctx"), _roster(), ws.config.orchestrate)
    ids = [a.node.id for a in plan.assigned]
    assert ids == ["hunt", "verify", "harvest", "prove"]
    by = {a.node.id: a for a in plan.assigned}
    assert by["harvest"].node.role == "implement" and by["harvest"].node.targets
    assert by["prove"].node.role == "verify" and by["prove"].node.deps == ("harvest",)
    for a in plan.assigned:
        assert a.node.output_schema is not None
        assert "FOREGROUND" in a.node.goal  # the single-shot instruction rides every node
    assert plan.est_total_usd > 0


def test_dry_run_prints_the_priced_plan_without_launching(state_home, git_workspace, monkeypatch):
    launched = []
    monkeypatch.setattr(ir, "run_route", lambda *a, **k: launched.append(1))
    rec = ir.run(git_workspace, scope="src/ctx", dry_run=True, budget_usd=0, node_timeout=60,
                 hosts=_roster())
    assert rec["dry_run"] and len(rec["plan"]) == 4 and not launched
    text = ir.render(rec)
    assert "dry run" in text and "hunt" in text and "prove" in text


def test_gate_is_promotable_only_when_all_three_hold():
    hunt = {"findings": [1, 2, 3, 4, 5]}
    verify = {"claimed": 5, "verified": [1, 2, 3, 4], "refuted": [5]}
    harvest = {"fixed": [1, 2, 3, 4], "skipped": []}
    prove = {"suite_passed": True, "failures": [], "lint_clean": True}
    g = ir.verdict(hunt, verify, harvest, prove)
    assert g["verdict"] == "promotable" and g["precision"] == 0.8
    # Precision below the bar holds the round even with a green suite.
    g = ir.verdict(hunt, {"claimed": 5, "verified": [1], "refuted": [2, 3, 4, 5]}, harvest, prove)
    assert g["verdict"] == "held" and "precision" in g["reasons"][0]
    # A red suite holds it even at precision 1.0.
    g = ir.verdict(hunt, {"claimed": 5, "verified": [1, 2, 3, 4, 5], "refuted": []}, harvest,
                   {"suite_passed": False, "failures": ["x"], "lint_clean": True})
    assert g["verdict"] == "held" and any("suite" in r for r in g["reasons"])


def test_gate_fails_closed_on_missing_yields():
    g = ir.verdict(None, None, None, None)
    assert g["verdict"] == "held" and g["precision"] == 0.0
    assert "hunt claimed nothing" in g["reasons"] and "suite did not pass" in g["reasons"][-2]


def test_no_host_is_an_error_not_a_crash(state_home, git_workspace):
    rec = ir.run(git_workspace, scope="src/ctx", dry_run=True, budget_usd=0, node_timeout=60, hosts=[])
    assert "error" in rec and "no installed" in ir.render(rec)
