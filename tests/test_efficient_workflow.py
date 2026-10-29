"""End-to-end acceptance for model delivery, progressive reads and durable repair."""
import asyncio
import json
import sys
import threading

import pytest

from conftest import make_ws, make_store
from ctx.sdk import ToolSession
from ctx.task_runtime import ExecutionLimits, TaskRuntime, ExecutionPaused
from ctx.edit_verification import Check, VerificationError, validate_verification
from ctx import anchors


def run(coro):
    return asyncio.run(coro)


def test_compact_view_preserves_status_streams_and_measures_actual_delivery(state_home, workspace_dir):
    session = ToolSession(workspace_dir)
    text = run(session.run_model([sys.executable, "-c", "import sys; print('left'); print('right',file=sys.stderr); sys.exit(4)"]))
    assert "exit=4" in text and "stdout:\nleft" in text and "stderr:\nright" in text
    store = make_store(make_ws(workspace_dir))
    try:
        row = json.loads((store.audit_dir / "tool-calls.jsonl").read_text())
        assert row["delivered_bytes"] == len(text.encode())
        assert row["ref"] in text
    finally:
        store.close()


def test_small_direct_read_large_outline_symbol_and_exact_range(state_home, workspace_dir):
    (workspace_dir / "small.py").write_text("def small():\n    return 17\n")
    (workspace_dir / "large.py").write_text("# filler\n" * 250 + "def target():\n    return 'needle'\n")
    session = ToolSession(workspace_dir)
    assert "L2:     return 17" in run(session.navigate("small.py"))
    outline = run(session.navigate("large.py"))
    assert "skeleton" in outline and "target" in outline and "L252:" not in outline
    assert "L252:     return 'needle'" in run(session.navigate("large.py", symbol="target"))
    assert "L252:     return 'needle'" in run(session.get("repo:large.py", start_line=252, end_line=252))


def test_reuse_only_complete_delivered_lines_changes_and_epoch_reset(state_home, workspace_dir):
    path = workspace_dir / "m.py"
    path.write_text("alpha\nbeta\ngamma\n")
    session = ToolSession(workspace_dir, context_epoch="conversation-1")
    first = run(session.get("repo:m.py", start_line=1, end_line=2))
    assert "L1: alpha" in first and "L2: beta" in first
    second = run(session.get("repo:m.py", start_line=1, end_line=3))
    assert "\nL1:" not in second and "\nL2:" not in second and "L3: gamma" in second
    assert "reused: 2" in second and "full:" in second
    path.write_text("alpha\nchanged\ngamma\n")
    changed = run(session.get("repo:m.py", start_line=1, end_line=3))
    assert "L2: changed" in changed and "\nL1:" not in changed and "\nL3:" not in changed
    session.set_context_epoch("conversation-2")
    reset = run(session.get("repo:m.py", start_line=1, end_line=3))
    assert all(f"L{n}:" in reset for n in range(1, 4))
    fresh = run(session.get("repo:m.py", start_line=1, end_line=3, fresh=True))
    assert all(f"L{n}:" in fresh for n in range(1, 4))


def test_hosts_without_epoch_signals_receive_full_reads(state_home, workspace_dir):
    (workspace_dir / "m.py").write_text("hello\n")
    session = ToolSession(workspace_dir)
    assert "L1: hello" in run(session.navigate("m.py"))
    assert "L1: hello" in run(session.navigate("m.py"))


def test_cancelled_read_never_earns_reuse_credit_on_resume(state_home, workspace_dir, monkeypatch):
    import ctx.toolviews as views
    (workspace_dir / "m.py").write_text("hello\n")
    original = views.navigate
    started, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        result = original(*args, **kwargs)
        started.set()
        assert release.wait(5)
        return result
    session = ToolSession(workspace_dir, durable=True, context_epoch="one")
    monkeypatch.setattr(views, "navigate", blocked)
    async def scenario():
        task = asyncio.create_task(session.navigate("m.py"))
        while not started.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())
    monkeypatch.setattr(views, "navigate", original)
    resumed = ToolSession(workspace_dir, task_id=session.task_id, context_epoch="one")
    assert "L1: hello" in run(resumed.navigate("m.py"))
    assert session.metrics["delivered_bytes"] == 0


def test_truncated_reads_do_not_mark_undelivered_lines_seen(state_home, workspace_dir):
    (workspace_dir / "m.py").write_text("".join(f"row-{n}-" + "x"*50 + "\n" for n in range(100)))
    session = ToolSession(workspace_dir, max_tokens=128, context_epoch="one")
    first = run(session.get("repo:m.py", start_line=1, end_line=100))
    second = run(session.get("repo:m.py", start_line=1, end_line=100))
    assert len(first.encode()) <= 512 and len(second.encode()) <= 512
    assert "omitted:" in first and "unseen lines" in first and "next:" in first
    assert first != second and "reused: 0" not in second


def test_durable_resume_preserves_handles_and_context_but_new_epoch_restores_text(state_home, workspace_dir, tmp_path):
    (workspace_dir / "m.py").write_text("hello\n")
    session = ToolSession(workspace_dir, durable=True, context_epoch="one")
    run(session.navigate("m.py"))
    resumed = ToolSession(workspace_dir, task_id=session.task_id, context_epoch="one")
    assert resumed.handles == session.handles
    assert "\nL1:" not in run(resumed.navigate("m.py"))
    restored = ToolSession(workspace_dir, task_id=session.task_id, context_epoch="two")
    assert "L1: hello" in run(restored.navigate("m.py"))
    report = run(restored.export(tmp_path / "evidence.tar"))
    assert not report.unresolved


def test_durable_command_identity_replays_without_mutating_twice(state_home, workspace_dir):
    argv = [sys.executable, "-c", "from pathlib import Path; p=Path('counter'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('done')"]
    session = ToolSession(workspace_dir, durable=True)
    first = run(session.run(argv, operation_key="patch-once"))
    resumed = ToolSession(workspace_dir, task_id=session.task_id)
    second = run(resumed.run(argv, operation_key="patch-once"))
    assert first == second and (workspace_dir / "counter").read_text() == "x"
    with pytest.raises(ValueError, match="different inputs"):
        run(resumed.run([sys.executable, "-c", "pass"], operation_key="patch-once"))


def test_shared_step_budget_counts_runs_and_reads(state_home, workspace_dir):
    (workspace_dir / "m.py").write_text("hello\n")
    session = ToolSession(workspace_dir, limits=ExecutionLimits(max_steps=2))
    run(session.run([sys.executable, "-c", "pass"]))
    run(session.navigate("m.py"))
    with pytest.raises(ExecutionPaused, match="step_budget"):
        run(session.search("repo:m.py", "hello"))


def test_composed_plan_charges_leaf_steps_and_cannot_execute_commands(state_home, workspace_dir):
    (workspace_dir / "m.py").write_text("def target():\n    return 42\n")
    session = ToolSession(workspace_dir, durable=True)
    plan = {"version": "ctx.plan/v1", "objective": {"kind": "diagnose", "question": "find target"},
            "steps": [{"id": "find", "op": "code.search", "args": {"pattern": "target"}}]}
    assert "target" in run(session.investigate(plan))
    ws = make_ws(workspace_dir); store = make_store(ws)
    try:
        rt = TaskRuntime(ws, store, session.task_id)
        assert rt.totals()["steps"] == 1
        assert next(iter(rt.state().operations.values()))["op"] == "code.search"
    finally:
        store.close()
    plan["steps"] = [{"id": "bad", "op": "test.run", "args": {"command": "touch must-not-run"}}]
    text = run(session.investigate(plan))
    assert "reject" in text.lower() or "not allowed" in text.lower() or "execute" in text.lower()
    assert not (workspace_dir / "must-not-run").exists()


def test_uncertain_mutation_blocks_automatic_retry_but_allows_observation(state_home, workspace_dir):
    from ctx import taskledger as ledger
    session = ToolSession(workspace_dir, durable=True, retry_failed=True, timeout=5)
    run(session.run([sys.executable, "-c", "pass"], operation_key="lost"))
    ws = make_ws(workspace_dir); store = make_store(ws)
    runtime = TaskRuntime(ws, store, session.task_id)
    row = next(iter(runtime.state().operations.values()))
    ledger.append(ws.root, dict(row, status="running"), durable=True)
    store.close()
    resumed = ToolSession(workspace_dir, task_id=session.task_id, retry_failed=True, timeout=5)
    with pytest.raises(ExecutionPaused, match="uncertain_mutation"):
        run(resumed.run([sys.executable, "-c", "pass"], operation_key="lost"))
    assert "ctx" in run(resumed.retrieve("stats", ref="repo:"))


def repair_fixture(workspace_dir):
    (workspace_dir / "m.py").write_text("value = 1\n")
    argv = (sys.executable, "-B", "-c", "from m import value; assert value == 2, value")
    request = {"schema": "ctx.edit-request/v1", "edits": [{"path": "m.py",
        "span": "1:1@" + anchors.anchor(["value = 1"]), "replacement": "value = 2\n"}]}
    return argv, request


def test_repair_joins_failure_edit_targeted_regression_and_exact_hashes(state_home, workspace_dir):
    argv, request = repair_fixture(workspace_dir)
    session = ToolSession(workspace_dir, durable=True)
    failure = run(session.run(argv))
    assert failure.command_exit_code == 1
    result = run(session.repair(failure.ref, request=request, targeted=[Check("behavior", argv)],
        regression=[Check("syntax", (sys.executable, "-m", "py_compile", "m.py"))], operation_key="repair-1"))
    assert result["outcome"] == "passed"
    assert [c["role"] for c in result["checks"]] == ["targeted", "regression"]
    ws = make_ws(workspace_dir); store = make_store(ws)
    try:
        validate_verification(ws, store, result["verificationRef"])
        runtime = TaskRuntime(ws, store, session.task_id)
        assert runtime.totals()["steps"] == 5  # initial failure, plan, apply, two checks
        (workspace_dir / "m.py").write_text("value = 3\n")
        with pytest.raises(VerificationError, match="changed|stale"):
            validate_verification(ws, store, result["verificationRef"])
    finally:
        store.close()


@pytest.mark.parametrize("bad_check", [Check("syntax", (sys.executable, "-c", "pass"), timeout=True),
                                      Check("behavior", "python -c pass")])
def test_invalid_repair_check_is_rejected_before_mutation(state_home, workspace_dir, bad_check):
    argv, request = repair_fixture(workspace_dir)
    session = ToolSession(workspace_dir)
    failure = run(session.run(argv))
    with pytest.raises(VerificationError):
        run(session.repair(failure.ref, request=request, targeted=[Check("behavior", argv)],
            regression=[bad_check]))
    assert (workspace_dir / "m.py").read_text() == "value = 1\n"


def test_repair_stops_at_failed_target_without_claiming_regression_success(state_home, workspace_dir):
    argv, request = repair_fixture(workspace_dir)
    request["edits"][0]["replacement"] = "value = 3\n"
    session = ToolSession(workspace_dir)
    failure = run(session.run(argv))
    result = run(session.repair(failure.ref, request=request, targeted=[Check("behavior", argv)],
        regression=[Check("syntax", (sys.executable, "-c", "open('should-not-run','w').write('bad')"))]))
    assert result["outcome"] == "failed" and len(result["checks"]) == 1
    assert not (workspace_dir / "should-not-run").exists()
