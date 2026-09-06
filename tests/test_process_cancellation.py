"""An interrupted owner must not leave its command tree running."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_store, make_ws
from ctx._proc import kill_and_reap, wait_or_kill


def _stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # A killed grandchild may wait for the container's PID 1 to reap it.
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        # Reaping can remove /proc after the first PID probe. In particular,
        # the loop may observe a zombie and the final assertion race its reap.
        # An unreadable /proc entry alone still does not prove termination.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False


@pytest.mark.parametrize("reaped", [False, True])
def test_stopped_rechecks_pid_when_proc_entry_disappears(monkeypatch, reaped):
    probes = []

    def probe(pid, sig):
        probes.append((pid, sig))
        if len(probes) == 2 and reaped:
            raise ProcessLookupError

    def vanished(_path):
        raise FileNotFoundError

    monkeypatch.setattr(os, "kill", probe)
    monkeypatch.setattr(Path, "read_text", vanished)
    assert _stopped(12345) is reaped
    assert probes == [(12345, 0), (12345, 0)]


@pytest.mark.parametrize("mode", ["wait", "capture", "wall", "idle"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_interruption_stops_descendants_and_preserves_exception(
    mode, error_type, tmp_path, monkeypatch, state_home, workspace_dir,
):
    from ctx.execution import run_capture
    from ctx.orchestrator import _run_bounded

    marker = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"with open({str(marker)!r}, 'w') as f: f.write(str(p.pid))\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    argv = [sys.executable, "-c", script]
    original_popen = subprocess.Popen
    processes = []
    interrupted = error_type("owner interrupted")

    def spawn(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        processes.append(proc)
        method = "communicate" if mode == "wall" else "wait"
        original = getattr(proc, method)

        def interrupt_once(*args, **kwargs):
            # Restore before raising so cleanup uses the real OS wait.
            setattr(proc, method, original)
            deadline = time.monotonic() + 10
            while not marker.exists() or not marker.read_text().strip():
                if time.monotonic() >= deadline:
                    pytest.fail("child did not become ready")
                time.sleep(0.01)
            raise interrupted

        setattr(proc, method, interrupt_once)
        return proc

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        with pytest.raises(error_type) as caught:
            if mode == "wait":
                proc = subprocess.Popen(argv, start_new_session=True,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wait_or_kill(proc, 30)
            elif mode == "capture":
                run_capture(ws, argv, store=store, timeout=30)
            else:
                _run_bounded(argv, cwd=workspace_dir, env=dict(os.environ),
                             timeout=30, idle_timeout=10 if mode == "idle" else 0)
        assert caught.value is interrupted
        proc = processes[-1]
        assert proc.returncode == -signal.SIGKILL
        grandchild = int(marker.read_text())
        deadline = time.monotonic() + 5
        while not _stopped(grandchild) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _stopped(grandchild), "descendant kept running after owner stopped"
        if mode in ("wall", "idle"):
            assert proc.stdout.closed and proc.stderr.closed
    finally:
        for proc in processes:
            kill_and_reap(proc)
