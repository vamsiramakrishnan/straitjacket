"""Live plan integration for evidence outcomes (debts 936231223f / 741c6afb40):

- ``execute_plan`` appends one ``ctx.plan-emission/v1`` line per executed ok
  node to ``.ctx-session-reads/plan-emissions.jsonl``, carrying the REAL
  source-state generation and per-node cost — fail-open, never in the digest;
- ``EvidenceOutcome`` gains ADDITIVE optional ``cost_ms``/``visible_tokens``
  whose absence keeps historical content-derived event ids byte-stable;
- ``compile_plan_value`` emits deterministic lower-median cost fields per
  operator only when events carry them, rendered as ints in the TOML.
"""

import json

from conftest import make_store, make_ws

from ctx.evidence_outcomes import make_event
from ctx.policy import compile_plan_value, compile_policy, render_policy

PLAN = {
    "version": "ctx.plan/v1",
    "objective": {"kind": "diagnose", "question": "q"},
    "steps": [{"id": "c", "op": "repo.changed"}],
}


def _make_kwargs(op="code.refs", salt=0, **extra):
    return dict(
        investigation_id=None,
        plan_node_id=None,
        operator=op,
        evidence_ids=(f"run:{salt:04d}",),
        match_classes=("exact_handle",),
        validation_associated=False,
        equivalent_requery=False,
        censored=False,
        generation_before="g0",
        generation_after="g1",
        actions_observed=2,
        **extra,
    )


def _write_ledger(ws_root, events):
    ldir = ws_root / ".ctx-session-reads"
    ldir.mkdir(parents=True, exist_ok=True)
    with (ldir / "evidence-followups.jsonl").open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e.payload(), sort_keys=True) + "\n")


# ------------------------------------------------- live emissions (plan_exec)


def test_execute_plan_appends_plan_emissions(state_home, git_workspace):
    from ctx import facts
    from ctx.plan_exec import execute_plan

    (git_workspace / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    # An uncommitted edit so repo.changed emits at least one row with a file.
    (git_workspace / "hello.py").write_text("print('changed')\n", encoding="utf-8")
    ws = make_ws(git_workspace)
    store = make_store(ws)
    text, code = execute_plan(ws, store, PLAN)
    assert code == 0

    path = git_workspace / ".ctx-session-reads" / "plan-emissions.jsonl"
    assert path.exists()
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["schema"] == "ctx.plan-emission/v1"
    assert rec["plan_node_id"] == "c"
    assert rec["op"] == "repo.changed"
    # The investigation id is real: the digest header names its prefix.
    assert rec["investigation_id"] and rec["investigation_id"][:12] in text
    # REAL generation (debt 936231223f), never an edit-count approximation.
    assert rec["generation"] == facts.current_generation(ws)
    assert "hello.py" in rec["files"]
    assert rec["rows"] >= 1
    for key in ("symbols", "tests", "handles"):
        assert isinstance(rec[key], list)
    assert isinstance(rec["duration_ms"], int) and rec["duration_ms"] >= 0
    assert isinstance(rec["visible_tokens"], int) and rec["visible_tokens"] > 0
    assert isinstance(rec["ts"], float)  # operational-only, like investigations


def test_plan_emissions_failure_never_affects_digest(state_home, git_workspace, monkeypatch):
    from ctx import plan_exec

    (git_workspace / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (git_workspace / "hello.py").write_text("print('changed')\n", encoding="utf-8")
    ws = make_ws(git_workspace)
    store = make_store(ws)
    baseline, code = plan_exec.execute_plan(ws, store, PLAN)
    assert code == 0

    def boom(*a, **kw):
        raise OSError("ledger dir unwritable")

    monkeypatch.setattr(plan_exec, "_append_plan_emissions", boom)
    text, code = plan_exec.execute_plan(ws, store, PLAN)
    assert code == 0  # fail-open: digest renders, exit code unchanged
    assert text.splitlines()[1:] == baseline.splitlines()[1:]  # only inv id differs


# --------------------------------------------- event-id stability (additive)


def test_event_id_stable_without_cost_fields():
    before = make_event(**_make_kwargs(salt=1))
    explicit_none = make_event(**_make_kwargs(salt=1, cost_ms=None, visible_tokens=None))
    assert before.event_id == explicit_none.event_id
    assert before.payload() == explicit_none.payload()
    # The keys are OMITTED (not null) so historical payload bytes are stable.
    assert "cost_ms" not in before.payload()
    assert "visible_tokens" not in before.payload()


def test_event_with_costs_carries_them_and_reids():
    plain = make_event(**_make_kwargs(salt=2))
    costed = make_event(**_make_kwargs(salt=2, cost_ms=120, visible_tokens=340))
    assert costed.payload()["cost_ms"] == 120
    assert costed.payload()["visible_tokens"] == 340
    assert costed.event_id != plain.event_id  # content-derived: cost is content


# --------------------------------------------- compiled medians (plan_value)


def test_compile_plan_value_emits_lower_medians(workspace_dir):
    events = [
        make_event(**_make_kwargs("repo.changed", salt=i, cost_ms=ms, visible_tokens=tok))
        for i, (ms, tok) in enumerate([(30, 300), (10, 100), (20, 200), (40, 400)])
    ]
    _write_ledger(workspace_dir, events)
    pv = compile_plan_value(make_ws(workspace_dir))
    row = pv["operators"]["repo.changed"]
    # Lower median of an even count: element (n-1)//2 of the sorted values.
    assert row["median_cost_ms"] == 20
    assert row["median_visible_tokens"] == 200
    assert pv["operators"]["*"]["median_cost_ms"] == 20  # fallback row too


def test_compile_plan_value_omits_medians_without_costs(workspace_dir):
    _write_ledger(workspace_dir, [make_event(**_make_kwargs("code.refs", salt=7))])
    pv = compile_plan_value(make_ws(workspace_dir))
    for op in ("code.refs", "*"):
        assert "median_cost_ms" not in pv["operators"][op]
        assert "median_visible_tokens" not in pv["operators"][op]


def test_render_policy_renders_medians_as_ints(state_home, workspace_dir):
    events = [
        make_event(**_make_kwargs("test.run", salt=i, cost_ms=15, visible_tokens=95))
        for i in range(3)
    ]
    _write_ledger(workspace_dir, events)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    pv = compile_plan_value(ws)
    toml_text = render_policy(compile_policy(store, ws, plan_value=pv))
    assert "median_cost_ms = 15" in toml_text
    assert "median_visible_tokens = 95" in toml_text
    import tomllib

    doc = tomllib.loads(toml_text)
    assert doc["plan_value"]["test.run"]["median_cost_ms"] == 15
