import json
import sys

import pytest

from conftest import make_ws, make_store
from ctx.task_runtime import TaskRuntime, ExecutionLimits, ExecutionPaused, OperationResult
from ctx.semantic.worker import WorkerResult


@pytest.fixture
def runtime(workspace_dir, state_home):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    rt = TaskRuntime.create(ws, store, goal="test", limits=ExecutionLimits(max_calls=2, max_cost_usd=.1))
    yield rt
    store.close()


def test_children_share_one_budget_and_replay_completed_operations(runtime):
    rt = runtime
    with rt.active():
        a, b = rt.child("a"), rt.child("b")
        a.perform("call", "model.test", {}, lambda t: OperationResult({"ok": True}), kind="model")
        b.perform("call", "model.test", {}, lambda t: OperationResult({"ok": True}), kind="model")
        with pytest.raises(ExecutionPaused, match="call_budget"):
            b.perform("third", "model.test", {}, lambda t: pytest.fail("overspent"), kind="model")
    resumed = TaskRuntime(rt.ws, rt.store, rt.task_id)
    with resumed.active():
        assert resumed.child("a").perform("call", "model.test", {}, lambda t: pytest.fail("replayed"), kind="model") == {"ok": True}
    assert resumed.totals()["unknown_cost_calls"] == 2
    assert resumed.totals()["admitted_cost_usd"] == .1


def test_failed_journal_write_prevents_dispatch(runtime, monkeypatch):
    from ctx import taskledger
    with runtime.active():
        original = taskledger.append
        def fail(root, row, **kw):
            if row["schema"] == taskledger.OPERATION_SCHEMA:
                raise OSError("disk full")
            return original(root, row, **kw)
        monkeypatch.setattr(taskledger, "append", fail)
        with pytest.raises(OSError):
            runtime.perform("x", "model.test", {}, lambda t: pytest.fail("launched without reservation"), kind="model")


def test_uncertain_time_is_not_released_and_mutations_are_not_repeated(runtime):
    def interrupt(t):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt), runtime.active():
        runtime.perform("write", "edit.apply", {}, interrupt, kind="mutate")
    resumed = TaskRuntime(runtime.ws, runtime.store, runtime.task_id, retry_failed=True)
    assert resumed.totals()["charged_seconds"] >= 60
    with resumed.active(), pytest.raises(ExecutionPaused, match="uncertain_mutation"):
        resumed.perform("write", "edit.apply", {}, lambda t: pytest.fail("repeated mutation"), kind="mutate")


def test_concurrent_coordinator_is_refused(runtime):
    other = TaskRuntime(runtime.ws, runtime.store, runtime.task_id)
    with runtime.active(), pytest.raises(ExecutionPaused, match="already_running"), other.active():
        pytest.fail("second coordinator")


def test_identity_mismatch_cannot_reuse_a_result(runtime):
    with runtime.active():
        runtime.perform("x", "read", {"ref": "a"}, lambda t: {})
        with pytest.raises(ValueError, match="different inputs"):
            runtime.perform("x", "read", {"ref": "b"}, lambda t: pytest.fail("bad reuse"))


def test_evidence_plan_and_semantic_map_share_a_runtime_without_controller(runtime):
    from ctx.plan_exec import execute_plan
    from ctx.semantic import prepare, run
    rt = runtime
    (rt.ws.root / "a.py").write_text("def f():\n    return 1\n")
    plan = {"version": "ctx.plan/v1", "objective": {"kind": "investigate", "question": "find f"},
            "steps": [{"id": "files", "op": "repo.files", "args": {}}]}
    request = {"question": "what returns a constant", "sources": ["repo:a.py"],
               "worker": {"identity": "fixture", "model": "fixture"}}
    def model(data, **kw):
        return WorkerResult(json.dumps({"schema": "ctx.semantic.response/v1", "findings": [], "unresolved": []}).encode())
    with rt.active():
        text, code = execute_plan(rt.ws, rt.store, plan, runtime=rt)
        assert code == 0, text
        _, report = run(rt.ws, rt.store, prepare(rt.ws, rt.store, request), worker=model, runtime=rt)
    assert report["status"] == "complete"
    assert {a["op"] for a in rt.state().operations.values()} == {"repo.files", "model.invoke"}
    assert rt.totals()["calls"] == 1


def test_bounded_command_capture_is_reusable_and_overflow_cannot_pass(runtime):
    from ctx.execution import run_capture
    with runtime.active():
        capture = run_capture(runtime.ws, [sys.executable, "-c", "print('x'*10000)"],
                              store=runtime.store, runtime=runtime, operation_key="test", output_bytes=100)
    assert capture.manifest["result"]["outputLimited"]
    assert capture.manifest["streams"]["stdout"]["bytes"] == 100


def test_retained_execution_roots_transitive_results_after_gc(runtime):
    from ctx.task_runtime import publish, read
    rt = runtime
    with rt.active():
        evidence = "blob:" + rt.store.put_blob(b"original observation\n")
        inner = publish(rt.store, {"source": evidence})
        rt.perform("nested", "example.observe", {}, lambda t: {"result": inner})
        rt.checkpoint("consumer", {"evidence": inner})
    root = rt.retain()
    # Keep only this root's lease, expiring every older artifact independently.
    latest = rt.store.db.execute("SELECT id FROM objects WHERE kind='execution' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    with rt.store.db:
        rt.store.db.execute("UPDATE objects SET created_at=0")
        rt.store.db.execute("DELETE FROM leases WHERE id != ?", (latest,))
    rt.store.gc(0)
    reopened = TaskRuntime(rt.ws, rt.store, rt.task_id)
    assert read(rt.store, reopened.checkpoint_value("consumer")["evidence"])["source"] == evidence
    assert rt.store.get_blob(evidence[5:]) == b"original observation\n"
    with reopened.active():
        assert reopened.perform("nested", "example.observe", {}, lambda t: pytest.fail("lost result")) == {"result": inner}


def test_custom_registered_operation_uses_shared_runtime(runtime):
    from ctx.plan_ops import register_op, OPS, payload
    from ctx.plan_exec import execute_plan
    register_op("fixture.observe", lambda pc, args, inp: payload("records", [{"answer": 42}]),
                input_kinds=(), output_kind="records")
    try:
        plan = {"version": "ctx.plan/v1", "objective": {"kind": "investigate", "question": "custom operator"},
                "steps": [{"id": "custom", "op": "fixture.observe", "args": {}}]}
        with runtime.active():
            assert execute_plan(runtime.ws, runtime.store, plan, runtime=runtime)[1] == 0
        assert next(iter(runtime.state().operations.values()))["op"] == "fixture.observe"
    finally:
        OPS.pop("fixture.observe")


def test_cancellation_reaches_a_process_that_closed_its_pipes(runtime):
    import threading
    import time
    from ctx.execution import run_capture
    timer = threading.Timer(.15, runtime.cancel)
    started = time.monotonic()
    with runtime.active():
        timer.start()
        try:
            result = run_capture(runtime.ws, [sys.executable, "-c", "import os,time;os.close(1);os.close(2);time.sleep(20)"],
                                 timeout=10, store=runtime.store, runtime=runtime, operation_key="cancel-test")
        finally:
            timer.join()
    assert result.manifest["result"]["cancelled"]
    assert time.monotonic() - started < 3


def test_root_limits_cannot_be_replaced_by_a_checkpoint(runtime):
    with runtime.active(), pytest.raises(ValueError, match="immutable"):
        runtime.checkpoint("execution", {"limits": {"max_calls": 9999}})
