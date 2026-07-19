"""Acceptance: ctx policy compile --plan-value — deterministic reviewable
priors from the evidence-outcome ledger."""

import json

from conftest import make_store, make_ws

from ctx.evidence_outcomes import make_event
from ctx.policy import (
    compile_plan_value,
    compile_policy,
    confidence_class,
    render_policy,
)


def _event(op, outcomes, censored=False, reasons=("exact_handle",), salt=0):
    return make_event(
        investigation_id=None,
        plan_node_id=None,
        evidence_ids=(f"run:{salt:04d}",),
        candidate_ids=(),
        downstream_action_kind="bash",
        downstream_action_ref=None,
        outcomes=outcomes,
        attribution_reasons=reasons,
        generation_before="g0",
        generation_after="g1",
        actions_observed=2 + salt,
        censored=censored,
        operator=op,
    )


def _write_ledger(ws_root, events):
    ldir = ws_root / ".ctx-session-reads"
    ldir.mkdir(parents=True, exist_ok=True)
    with (ldir / "evidence-outcomes.jsonl").open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e.payload(), sort_keys=True) + "\n")


def test_compile_plan_value_rates_and_censoring(workspace_dir):
    events = [
        _event("evidence.failing_in_changed", ("landed", "narrowed"), salt=1),
        _event("evidence.failing_in_changed", ("landed", "validated_after_edit"), salt=2),
        _event("evidence.failing_in_changed", ("redundant",), salt=3),
        _event("evidence.failing_in_changed", ("landed",), censored=True, salt=4),
    ]
    _write_ledger(workspace_dir, events)
    pv = compile_plan_value(make_ws(workspace_dir))
    row = pv["operators"]["evidence.failing_in_changed"]
    assert row["observations"] == 4 and row["censored"] == 1
    # Positive denominators = all observations (censored under-counts only).
    assert row["landing_rate"] == round(3 / 4, 2)
    # Negative denominators exclude censored observations.
    assert row["redundancy_rate"] == round(1 / 3, 2)
    assert row["confidence"] == "insufficient"  # < 5 observations
    # Global fallback row aggregates everything.
    assert pv["operators"]["*"]["observations"] == 4


def test_compilation_is_deterministic_and_dedupes(state_home, workspace_dir):
    events = [_event("code.refs", ("landed",), salt=i) for i in range(6)]
    _write_ledger(workspace_dir, events)
    _write_ledger(workspace_dir, events)  # duplicate append: same event_ids
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    pv1, pv2 = compile_plan_value(ws), compile_plan_value(ws)
    assert pv1 == pv2
    assert pv1["operators"]["code.refs"]["observations"] == 6  # deduped
    p1 = render_policy(compile_policy(store, ws, plan_value=pv1))
    p2 = render_policy(compile_policy(store, ws, plan_value=pv2))
    assert p1 == p2  # byte-identical TOML
    assert "[plan_value]" in p1 and '[plan_value."code.refs"]' in p1
    import tomllib

    doc = tomllib.loads(p1)  # the rendered policy stays valid TOML
    assert doc["plan_value"]["code.refs"]["observations"] == 6


def test_epoch_unchanged_without_plan_value(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    base = compile_policy(store, ws)
    with_empty = compile_policy(store, ws, plan_value={"operators": {}})
    assert base["epoch"] == with_empty["epoch"]  # additive: empty table = old id


def test_confidence_classes_thresholds():
    assert confidence_class(0) == "insufficient"
    assert confidence_class(4) == "insufficient"
    assert confidence_class(5) == "low"
    assert confidence_class(19) == "low"
    assert confidence_class(20) == "medium"
    assert confidence_class(49) == "medium"
    assert confidence_class(50) == "high"


def test_counts_ride_with_rates(workspace_dir):
    _write_ledger(workspace_dir, [_event("semantic.taint", ("redundant",), salt=9)])
    pv = compile_plan_value(make_ws(workspace_dir))
    row = pv["operators"]["semantic.taint"]
    assert {"observations", "attributed", "censored"} <= set(row)
    assert all(k.endswith("_rate") or not k.endswith("rate") for k in row)
