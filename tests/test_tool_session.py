"""Public tool boundary: status, bounded delivery, cancellation and portability."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from conftest import make_ws, make_store
from ctx.sdk import ToolSession


def run(coro):
    return asyncio.run(coro)


def test_complete_small_streams_keep_identity_and_no_final_newline(state_home, workspace_dir):
    session = ToolSession(workspace_dir)
    result = run(session.run([sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.stderr.write('warning'); sys.exit(4)"]))
    assert result.delivery == "inline"
    assert result.stdout == "hello" and result.stderr == "warning"
    assert result.command_exit_code == result.native_exit_code == 4
    assert result.signal is None and not result.timed_out
    assert result.ref in session.handles
    assert "hello" in run(session.get(result.ref + "#stdout", start_line=1, end_line=1))


def test_empty_output_is_complete_success(state_home, workspace_dir):
    result = run(ToolSession(workspace_dir).run([sys.executable, "-c", "pass"]))
    assert result.command_exit_code == 0
    assert result.delivery == "inline" and result.stdout == result.stderr == ""


def test_flood_keeps_quiet_evidence_retrievable(state_home, workspace_dir):
    session = ToolSession(workspace_dir)
    code = "for i in range(5000): print('quiet-needle-17' if i == 2136 else 'ordinary-output-' + str(i))"
    result = run(session.run([sys.executable, "-c", code]))
    assert result.delivery == "digest"
    assert len(result.to_json()) < 6000 and "omitted" in result.digest
    text = run(session.get(result.ref + "#stdout", start_line=2137, end_line=2137))
    assert "quiet-needle-17" in text
    assert "quiet-needle-17" in run(session.search(result.ref, "quiet-needle"))


def test_inline_is_sanitized_without_altering_stored_bytes(state_home, workspace_dir):
    result = run(ToolSession(workspace_dir).run([sys.executable, "-c", "print('ghp_' + 'a' * 40)"]))
    assert "a" * 40 not in result.stdout and result.redactions
    store = make_store(make_ws(workspace_dir))
    try:
        manifest = store.get_manifest(result.ref[4:])
        assert b"a" * 40 in store.get_blob(manifest["streams"]["stdout"]["blob"][7:])
    finally:
        store.close()


def test_binary_is_never_inlined(state_home, workspace_dir):
    result = run(ToolSession(workspace_dir).run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0, 1, 2]))"]))
    assert result.delivery == "digest" and "\x00" not in result.to_json()


def test_budget_and_line_limits_prevent_inline(state_home, workspace_dir):
    session = ToolSession(workspace_dir, max_tokens=64)
    result = run(session.run([sys.executable, "-c", "print('z' * 3000)"]))
    assert result.delivery == "digest" and len(result.to_json()) < 1300
    (workspace_dir / "ctx.toml").write_text("version=1\n[budgets]\nmax_inline_lines=2\n")
    result = run(session.run([sys.executable, "-c", "print('a\\nb\\nc')"]))
    assert result.delivery == "digest"


def test_signal_is_not_success_or_exit_one(state_home, workspace_dir):
    result = run(ToolSession(workspace_dir).run([sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"]))
    assert result.command_exit_code is None and result.signal == "SIGTERM"
    assert result.native_exit_code == 143


def test_timeout_records_partial_output_and_typed_termination(state_home, workspace_dir):
    session = ToolSession(workspace_dir, timeout=2)
    result = run(session.run([sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"], timeout_seconds=0.2))
    assert result.timed_out and result.native_exit_code == 124
    assert result.command_exit_code is None
    assert "partial" in run(session.get(result.ref, start_line=1, end_line=1))


def test_async_cancellation_stops_descendants_and_publishes_evidence(state_home, workspace_dir):
    from test_process_cancellation import _wait_stopped
    marker = workspace_dir / "child.pid"
    script = ("import subprocess,sys,time\n"
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
              f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
              "print('before-cancel',flush=True)\ntime.sleep(30)\n")
    session = ToolSession(workspace_dir, timeout=10)
    async def scenario():
        task = asyncio.create_task(session.run([sys.executable, "-c", script]))
        for _ in range(200):
            if marker.exists() and marker.read_text().strip():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("command did not start")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _wait_stopped(int(marker.read_text()))
        assert session.handles
        assert "before-cancel" in await session.get(session.handles[0], start_line=1, end_line=1)
    run(scenario())
    store = make_store(make_ws(workspace_dir))
    try:
        manifest = store.get_manifest(session.handles[0][4:])
        assert manifest["result"]["cancelled"] is True
        row = json.loads((store.audit_dir / "tool-calls.jsonl").read_text().splitlines()[0])
        assert row["cancelled"] is True
    finally:
        store.close()


def test_parallel_tools_do_not_mutate_cwd_or_environment(state_home, workspace_dir):
    (workspace_dir / "one").mkdir(); (workspace_dir / "two").mkdir()
    initial = Path.cwd()
    session = ToolSession(workspace_dir, env={"SJ_TEST_VALUE": "child-only"})
    async def scenario():
        return await asyncio.gather(*(session.run([sys.executable, "-c", "import os; print(os.path.basename(os.getcwd()), os.environ['SJ_TEST_VALUE'])"], working_directory=d) for d in ("one", "two")))
    results = run(scenario())
    assert [r.stdout for r in results] == ["one child-only\n", "two child-only\n"]
    assert Path.cwd() == initial and "SJ_TEST_VALUE" not in os.environ


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_before_launch(value, workspace_dir):
    with pytest.raises(ValueError):
        ToolSession(workspace_dir, timeout=value)


def test_refuses_escape_and_mutation_through_retrieval(state_home, workspace_dir):
    session = ToolSession(workspace_dir)
    with pytest.raises(Exception, match="outside"):
        run(session.run([sys.executable, "-c", "pass"], working_directory=".."))
    with pytest.raises(ValueError, match="read operation"):
        run(session.retrieve("run", options={"command": "touch bad"}))
    with pytest.raises(ValueError):
        run(session.run(""))
    assert not (workspace_dir / "bad").exists()


def test_session_root_stays_confined_when_cli_policy_allows_outside(state_home, workspace_dir, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "value.txt").write_text("outside-evidence")
    (workspace_dir / "escape").symlink_to(outside, target_is_directory=True)
    (workspace_dir / "ctx.toml").write_text("version=1\n[workspace]\nallow_outside_root=true\n")
    session = ToolSession(workspace_dir)
    with pytest.raises(Exception, match="outside|escapes"):
        run(session.run([sys.executable, "-c", "pass"], working_directory=str(outside)))
    with pytest.raises(Exception, match="outside|escapes"):
        run(session.get("repo:escape/value.txt", start_line=1, end_line=1))


def test_capsule_round_trip_without_private_store_copy(state_home, workspace_dir, tmp_path):
    from ctx.capsule import import_capsule, verify
    from ctx.store import Store
    session = ToolSession(workspace_dir)
    result = run(session.run([sys.executable, "-c", "print('portable-evidence')"]))
    path = tmp_path / "evidence.tar"
    report = run(session.export(path))
    assert report.manifests >= 1 and not report.unresolved
    assert verify(path)[2] == []
    store = Store("other-workspace", state_root=tmp_path / "receiver")
    try:
        import_capsule(store, path)
        manifest = store.get_manifest(result.ref[4:])
        assert b"portable-evidence" in store.get_blob(manifest["streams"]["stdout"]["blob"][7:])
    finally:
        store.close()


def test_telemetry_measures_wire_envelope_without_claiming_compression(state_home, workspace_dir):
    result = run(ToolSession(workspace_dir).run([sys.executable, "-c", "print('tiny')"]))
    store = make_store(make_ws(workspace_dir))
    try:
        row = json.loads((store.audit_dir / "tool-calls.jsonl").read_text().strip())
        assert row["captured_bytes"] == 5
        assert row["delivered_bytes"] == len(result.to_json().encode())
        assert row["expanded_bytes"] > 0
        assert "tiny" not in json.dumps(row)
        rows = [json.loads(line) for line in (store.audit_dir / "telemetry.jsonl").read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["emitted_bytes"] == row["delivered_bytes"]
    finally:
        store.close()


def test_shim_tools_use_public_runtime_and_preserve_quoted_patterns(state_home, workspace_dir):
    path = Path(__file__).resolve().parents[1] / "contrib/ctx-agy/ctx_agy.py"
    spec = importlib.util.spec_from_file_location("shim", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    tools = {f.__name__: f for f in module.make_tools(workspace_dir, profile="full")}
    assert all(asyncio.iscoroutinefunction(f) for f in tools.values())
    (workspace_dir / "empty.py").touch()
    (workspace_dir / "with space.py").write_text("def hello():\n    return 'quoted value'\n")
    assert "empty.py" in run(tools["find_file"]("*.py"))
    assert "quoted value" in run(tools["ctx_search"]("repo:", "quoted value"))
    command = json.loads(run(tools["run_command"]("printf tiny")))
    assert command["stdout"] == "tiny" and command["command_exit_code"] == 0
    assert len(module.make_tools(workspace_dir)) == 3
