import pytest
from ctx.edit_policy import choose_format


def rows(n=60):
    return [{"case": str(i), "caseHash": "case-" + str(i), "repeat": 0, "format": fmt, "model": "m", "shape": "mechanical",
             "measurement": "live", "task_success": True, "wrong_target": False,
             "cost_usd": cost}
            for i in range(n) for fmt, cost in [("native", 1.0), ("anchored", 0.5)]]


def test_policy_requires_paired_quality_cost_and_distinct_cases():
    assert choose_format(rows(), model="m", shape="mechanical")["format"] == "anchored"
    assert choose_format(rows(10), model="m", shape="mechanical")["format"] == "native"
    assert choose_format(rows(), model="different", shape="mechanical")["format"] == "native"
    repeated = rows()
    for i, row in enumerate(repeated):
        row["case"], row["repeat"] = "same", i // 2
    assert choose_format(repeated, model="m", shape="mechanical")["format"] == "native"


@pytest.mark.parametrize("field,value", [("measurement", "fixture"), ("wrong_target", True),
    ("cost_usd", None), ("cost_usd", float("nan")), ("cost_usd", 2.0)])
def test_policy_refuses_incomplete_or_worse_evidence(field, value):
    sample = rows()
    for row in sample:
        if row["format"] == "anchored":
            row[field] = value
    assert choose_format(sample, model="m", shape="mechanical")["format"] == "native"


def test_successes_cannot_hide_paired_regressions():
    sample = rows()
    for row in sample[:20]:
        if row["format"] == "anchored":
            row["task_success"] = False
    assert choose_format(sample, model="m", shape="mechanical")["format"] == "native"
    assert choose_format(rows() + [rows()[0]], model="m", shape="mechanical")["reason"] == "invalid_or_duplicate_observations"


def test_prewalk_uses_total_cost_for_the_exact_model_pair():
    from ctx.edit_policy import choose_prewalk
    sample = rows()
    for row in sample:
        row["model"] = "guide->executor"
        row["format"] = "frontier" if row["format"] == "native" else "prewalk"
    assert choose_prewalk(sample, guide_model="guide", executor_model="executor", shape="mechanical")["strategy"] == "prewalk"
    assert choose_prewalk(sample, guide_model="guide", executor_model="other", shape="mechanical")["strategy"] == "frontier"
    for row in sample:
        if row["format"] == "prewalk":
            row["cost_usd"] = 1.5  # Cheap execution did not recover guide/handoff cost.
    assert choose_prewalk(sample, guide_model="guide", executor_model="executor", shape="mechanical")["strategy"] == "frontier"


def test_orchestration_policy_pins_advice_for_one_launch(state_home, git_workspace):
    import json
    from dataclasses import replace
    from conftest import make_ws
    from test_task_ledger_orchestration import _hosts, _usage
    from ctx.orchestrator import build_route_plan, run_route
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, edit_policy_file="policy.jsonl")
    raw = {"nodes": [{"id": "edit", "goal": "fix", "role": "implement", "edit_shape": "mechanical",
                       "min_tier": "frontier", "deps": []}]}
    raw["nodes"].append({"id": "verify", "goal": "check", "role": "verify", "min_tier": "economy", "deps": ["edit"]})
    plan = build_route_plan("fix", raw, _hosts("claude"), cfg)
    sample = rows()
    for row in sample:
        row["model"] = plan.assigned[0].model.id
    (git_workspace / "policy.jsonl").write_text("\n".join(json.dumps(r) for r in sample))
    prompts = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        prompts.append(prompt)
        return 0, "done", "", _usage()

    run_route(ws, plan, cfg, launch=launch)
    assert len(prompts) == 2
    assert sum("policy for this attempt: anchored" in p for p in prompts) == 1


def test_configured_prewalk_policy_without_evidence_does_not_arm(state_home, git_workspace):
    from dataclasses import replace
    from conftest import make_ws
    from test_task_ledger_orchestration import _hosts, _usage, _PREWALK_RAW
    from ctx.orchestrator import build_route_plan, run_route
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, prewalk=True, prewalk_policy_file="empty.jsonl")
    (git_workspace / "empty.jsonl").write_text("")
    plan = build_route_plan("fix", _PREWALK_RAW, _hosts("claude"), cfg)
    prompts = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        prompts.append(prompt)
        return 0, "done", "", _usage()

    run_route(ws, plan, cfg, launch=launch)
    assert all("Prewalk:" not in p for p in prompts)
