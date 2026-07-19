"""Evidence-plan IR: parsing, static validation (typed rejections), pricing.

The validator is the totality proof — every check here is static, and a
plan that validates cannot fail for a structural reason at execution time.
"""

import json

import pytest


def _plan(steps, budget=None, question="why?", emit=None):
    doc = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": question},
        "budget": budget or {"wall_seconds": 60},
        "steps": steps,
    }
    if emit is not None:
        doc["emit"] = emit
    return doc


def _validate(doc, **kw):
    from ctx.plan_ir import parse_plan, validate_plan

    return validate_plan(parse_plan(doc), **kw)


def _reasons(rejections):
    return [r.reason for r in rejections]


def test_minimal_plan_validates():
    doc = _plan([{"id": "changes", "op": "repo.changed"}])
    assert _validate(doc) == []


def test_rejections_are_closed_vocabulary():
    from ctx.plan_ir import REJECTION_VOCABULARY, Rejection

    with pytest.raises(ValueError):
        Rejection("made_up_reason", None, "x")
    assert "forward_reference" in REJECTION_VOCABULARY


def test_bad_json_raises_plan_error():
    from ctx.plan_ir import PlanError, parse_plan

    with pytest.raises(PlanError):
        parse_plan("{not json")
    with pytest.raises(PlanError):
        parse_plan(json.dumps({"version": "ctx.plan/v1", "steps": []}))


def test_unknown_op_and_duplicate_id():
    doc = _plan(
        [
            {"id": "a", "op": "repo.changed"},
            {"id": "a", "op": "repo.changed"},
            {"id": "b", "op": "no.such.op"},
        ]
    )
    reasons = _reasons(_validate(doc))
    assert "duplicate_id" in reasons
    assert "unknown_op" in reasons


def test_forward_reference_subsumes_cycles():
    doc = _plan(
        [
            {"id": "a", "op": "evidence.count", "input": "b"},
            {"id": "b", "op": "repo.changed"},
        ]
    )
    assert "forward_reference" in _reasons(_validate(doc))


def test_kind_mismatch_is_static():
    # ast.outline needs files|sites; evidence.join emits records.
    doc = _plan(
        [
            {"id": "j", "op": "evidence.join", "args": {"on": "shared_cause_groups"}},
            {"id": "o", "op": "ast.outline", "input": "j"},
        ]
    )
    assert "kind_mismatch" in _reasons(_validate(doc))


def test_source_rejects_input_unless_foreach():
    doc = _plan(
        [
            {"id": "changes", "op": "repo.changed"},
            {"id": "s", "op": "code.search", "args": {"pattern": "x"}, "input": "changes"},
        ]
    )
    assert "source_takes_no_input" in _reasons(_validate(doc))
    doc2 = _plan(
        [
            {"id": "changes", "op": "repo.changed"},
            {
                "id": "s",
                "op": "code.search",
                "args": {"pattern": "{item}"},
                "input": "changes",
                "foreach": "file",
                "cap": 4,
            },
        ]
    )
    assert _validate(doc2) == []


def test_foreach_requires_cap_and_respects_ceiling():
    base = {
        "id": "s",
        "op": "code.search",
        "args": {"pattern": "{item}"},
        "input": "changes",
        "foreach": "file",
    }
    doc = _plan([{"id": "changes", "op": "repo.changed"}, dict(base)])
    assert "fanout_uncapped" in _reasons(_validate(doc))
    doc = _plan([{"id": "changes", "op": "repo.changed"}, dict(base, cap=10_000)])
    assert "fanout_cap_exceeded" in _reasons(_validate(doc))


def test_node_budget_ceiling():
    # 30 distinct ids, over the hard cap of 24.
    steps = [
        {"id": f"n{chr(97 + i // 10)}{chr(97 + i % 10)}", "op": "repo.changed"}
        for i in range(30)
    ]
    assert "node_budget_exceeded" in _reasons(_validate(_plan(steps)))


def test_guard_grammar_is_micro():
    doc = _plan(
        [
            {"id": "t", "op": "evidence.join", "args": {"on": "shared_cause_groups"}},
            {"id": "g", "op": "repo.changed", "when": "t.count > 0"},
        ]
    )
    assert _validate(doc) == []
    for bad in ("t.count > x", "len(t) > 0", "t.rows[0] == 1", "__import__('os')"):
        doc = _plan(
            [
                {"id": "t", "op": "evidence.join", "args": {"on": "shared_cause_groups"}},
                {"id": "g", "op": "repo.changed", "when": bad},
            ]
        )
        assert "guard_grammar" in _reasons(_validate(doc)), bad


def test_guard_forward_reference():
    doc = _plan([{"id": "g", "op": "repo.changed", "when": "later.count > 0"}])
    assert "forward_reference" in _reasons(_validate(doc))


def test_execute_class_rejected_on_mcp_tier():
    doc = _plan([{"id": "t", "op": "test.run", "args": {"command": "true"}}])
    assert _validate(doc, tier="cli") == []
    assert "execute_on_observe_tier" in _reasons(_validate(doc, tier="mcp"))
    for op, args in (
        ("ast.rewrite.preview", {"pattern": "a", "rewrite": "b"}),
    ):
        doc = _plan([{"id": "r", "op": op, "args": args}])
        assert "execute_on_observe_tier" in _reasons(_validate(doc, tier="mcp"))


def test_rewrite_requires_engine_when_on_missing_fail(monkeypatch):
    from ctx import astgrep

    astgrep.binary.cache_clear()
    monkeypatch.setenv("PATH", "/nonexistent")
    doc = _plan(
        [{"id": "r", "op": "ast.rewrite.preview", "args": {"pattern": "a", "rewrite": "b"}}]
    )
    try:
        assert "engine_unavailable" in _reasons(_validate(doc))
    finally:
        astgrep.binary.cache_clear()


def test_bad_args_are_typed():
    doc = _plan([{"id": "j", "op": "evidence.join", "args": {"on": "nope"}}])
    assert "bad_args" in _reasons(_validate(doc))
    doc = _plan([{"id": "s", "op": "code.search"}])
    assert "bad_args" in _reasons(_validate(doc))


def test_q_pipe_kind_chain_checked_statically():
    doc = _plan([{"id": "q", "op": "q.pipe", "args": {"query": "files | count"}}])
    assert "bad_args" in _reasons(_validate(doc))  # files can't open a pipeline
    doc = _plan(
        [{"id": "q", "op": "q.pipe", "args": {"query": "search TODO | files | count"}}]
    )
    assert _validate(doc) == []


def test_price_is_deterministic_and_shows_the_bill():
    from ctx.plan_ir import parse_plan, price_plan

    doc = _plan(
        [
            {"id": "changes", "op": "repo.changed"},
            {"id": "t", "op": "test.run", "args": {"command": "true"}},
        ]
    )
    plan = parse_plan(doc)
    a, b = price_plan(plan), price_plan(plan)
    assert a == b
    assert "1 model round" in a and "test ≈ 120u" in a


def test_plan_id_is_content_addressed():
    from ctx.plan_ir import parse_plan

    doc = _plan([{"id": "changes", "op": "repo.changed"}])
    assert parse_plan(doc).plan_id() == parse_plan(json.loads(json.dumps(doc))).plan_id()
    doc2 = _plan([{"id": "changes", "op": "repo.changed"}], question="different?")
    assert parse_plan(doc).plan_id() != parse_plan(doc2).plan_id()
