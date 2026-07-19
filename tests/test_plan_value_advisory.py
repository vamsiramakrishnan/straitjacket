"""Acceptance: advisory coverage is conditioned on REALIZED rows — a node
that returned 0 rows contributes nothing, so a floor only satisfiable by
an empty join stays UNMET and the receipt says the acquisition continues
(debt e03586e148)."""

from conftest import make_store, make_ws

from ctx import plan_ir
from ctx import plan_value as pv

# The live-spin shape: the only causality provider is an evidence.join.
PLAN = {
    "version": "ctx.plan/v1",
    "objective": {
        "kind": "diagnose",
        "question": "which changed symbols fail?",
        "requires": [{"dimension": "causality", "floor": 0.8}],
    },
    "steps": [
        {"id": "changes", "op": "repo.changed"},
        {"id": "culprits", "op": "evidence.join",
         "args": {"on": "failing_in_changed"}, "after": ["changes"]},
    ],
}


def test_realized_coverage_requires_rows():
    steps = plan_ir.parse_plan(PLAN).steps
    # 0 rows: the join's declared causality=1.0 earns nothing.
    empty = pv.realized_coverage(steps, {"changes": 1, "culprits": 0})
    assert "causality" not in empty
    assert empty["changedness"] == 1.0  # changes DID land rows
    # >=1 row: the declared provides count.
    full = pv.realized_coverage(steps, {"changes": 1, "culprits": 3})
    assert full["causality"] == 1.0
    # A node absent from node_rows (skipped/errored) counts as empty.
    assert "causality" not in pv.realized_coverage(steps, {"changes": 1})


def test_empty_join_keeps_floor_unmet_and_advisory_continues(
    git_workspace, state_home
):
    """Integration: execute the plan for real (clean tree, no captured
    runs → the join is ok with 0 rows), then assert the receipt over the
    realized coverage marks the causality floor UNMET — where the old
    declared-provides estimate said the floors were satisfied."""
    from ctx import plan_ops
    from ctx.plan_exec import execute_plan

    ws = make_ws(git_workspace)
    store = make_store(ws)
    node_rows: dict[str, int] = {}
    text, code = execute_plan(ws, store, PLAN, node_rows=node_rows)
    assert code == 0
    assert node_rows.get("culprits") == 0  # executed ok, empty

    plan = plan_ir.parse_plan(PLAN)
    floors = pv.required_floors(plan.objective_kind, plan.requires)
    # The bug being fixed: declared provides alone satisfy the floor.
    declared = pv.apply_expected_coverage(plan_ops.OPS["evidence.join"].provides, {})
    assert declared["causality"] >= floors["causality"]
    # Realized coverage does not — the advisory must keep going.
    coverage = pv.realized_coverage(plan.steps, node_rows)
    stop, receipt = pv.stopping_decision([], coverage, floors)
    assert not stop
    assert "evidence acquisition continues" in receipt
    assert "UNMET" in receipt
