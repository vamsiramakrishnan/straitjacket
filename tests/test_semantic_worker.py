"""Real-process transport tests; no provider calls or quality claims."""
import json
import sys
import time

import pytest

from ctx.semantic.worker import CommandWorker


def test_worker_uses_json_stdin_and_a_fresh_working_directory(tmp_path):
    worker = CommandWorker([sys.executable, "-c", "import os,sys; print(os.getcwd(), file=sys.stderr); sys.stdout.buffer.write(sys.stdin.buffer.read())"])
    result = worker(b'{"evidence":"kept"}', timeout=2, response_bytes=1024)
    assert result.error is None
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"evidence": "kept"}
    assert "ctx-semantic-" in result.stderr.decode()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_worker_flood_is_capped_and_stopped(stream):
    worker = CommandWorker([sys.executable, "-c", f"import sys; sys.{stream}.write('x'*10000000)"])
    result = worker(b"{}", timeout=2, response_bytes=100)
    assert result.error == "output_limit"
    assert len(getattr(result, stream)) == 100


def test_timeout_includes_process_that_closes_pipes_then_sleeps():
    worker = CommandWorker([sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(10)"])
    started = time.monotonic()
    result = worker(b"{}", timeout=0.15, response_bytes=100)
    assert result.error == "timeout"
    assert time.monotonic() - started < 3


def test_leader_exit_with_pipe_holding_descendant_still_times_out():
    worker = CommandWorker([sys.executable, "-c", "import os,time; pid=os.fork(); time.sleep(10) if pid == 0 else None"])
    result = worker(b"{}", timeout=0.15, response_bytes=100)
    assert result.error == "timeout"


def test_missing_command_is_a_failed_attempt():
    result = CommandWorker(["/definitely/not/a/worker"])(b"{}", timeout=1, response_bytes=100)
    assert result.error == "launch_failed"


def test_transport_interruption_uses_shared_process_cleanup(monkeypatch):
    from ctx.semantic import worker as module
    cleaned = []
    original = module.kill_and_reap
    def cleanup(proc):
        original(proc)
        cleaned.append(proc.returncode)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(module, "kill_and_reap", cleanup)
    monkeypatch.setattr(module.selectors.DefaultSelector, "select", interrupt)
    with pytest.raises(KeyboardInterrupt):
        CommandWorker([sys.executable, "-c", "import time;time.sleep(10)"])(b"{}", timeout=1, response_bytes=100)
    assert cleaned and cleaned[0] is not None
