"""Acceptance: ctx policy compile --plan-value — deterministic per-operator
follow-up COUNTS table (rates and Wilson bounds are derived at read time,
never committed)."""

import json

from conftest import make_store, make_ws

from ctx.evidence_outcomes import make_event
from ctx.policy import compile_plan_value, compile_policy, render_policy


def _event(op, *, used=(), validation=False, requery=False, censored=False,
           cost_ms=None, visible_tokens=None, salt=0):
    return make_event(
        investigation_id=None,
        plan_node_id=None,
        operator=op,
        evidence_ids=(f"run:{salt:04d}",),
        match_classes=used,
        validation_associated=validation,
        equivalent_requery=requery,
        censored=censored,
        generation_before="g0",
        generation_after="g1",
        actions_observed=2 + salt,
        cost_ms=cost_ms,
        visible_tokens=visible_tokens,
    )


def _write_ledger(ws_root, events):
    ldir = ws_root / ".ctx-session-reads"
    ldir.mkdir(parents=True, exist_ok=True)
    with (ldir / "evidence-followups.jsonl").open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e.payload(), sort_keys=True) + "\n")


def test_compile_counts_not_rates(workspace_dir):
    events = [
        _event("evidence.join", used=("exact_handle",), salt=1),
        _event("evidence.join", used=("exact_test_id",), validation=True, salt=2),
        _event("evidence.join", requery=True, salt=3),
        _event("evidence.join", used=("exact_file",), censored=True, salt=4),
    ]
    _write_ledger(workspace_dir, events)
    pv = compile_plan_value(make_ws(workspace_dir))
    row = pv["operators"]["evidence.join"]
    assert row == {
        "observations": 4,
        "used_exactly": 3,
        "validation_associated": 1,
        "equivalent_requery": 1,
        "censored": 1,
    }
    assert not any(k.endswith("_rate") for k in row)  # counts only, no rates
    assert "confidence" not in row  # Wilson at read time replaces classes
    assert pv["operators"]["*"]["observations"] == 4  # global fallback row


def test_compilation_is_deterministic_and_dedupes(state_home, workspace_dir):
    events = [_event("code.refs", used=("exact_symbol",), salt=i) for i in range(6)]
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
    assert "association, not" in p1  # honest language in the committed artifact
    import tomllib

    doc = tomllib.loads(p1)
    assert doc["plan_value"]["code.refs"]["used_exactly"] == 6


def test_cost_medians_only_when_observed(workspace_dir):
    events = [
        _event("semantic.taint", used=("exact_file",), cost_ms=1800,
               visible_tokens=121, salt=1),
        _event("semantic.taint", used=("exact_file",), cost_ms=1500,
               visible_tokens=100, salt=2),
        _event("code.refs", used=("exact_symbol",), salt=3),  # no cost fields
    ]
    _write_ledger(workspace_dir, events)
    pv = compile_plan_value(make_ws(workspace_dir))
    taint = pv["operators"]["semantic.taint"]
    assert taint["median_cost_ms"] == 1500  # lower median of observed values
    assert "median_cost_ms" not in pv["operators"]["code.refs"]


def test_epoch_unchanged_without_plan_value(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    base = compile_policy(store, ws)
    with_empty = compile_policy(store, ws, plan_value={"operators": {}})
    assert base["epoch"] == with_empty["epoch"]  # additive: empty table = old id
