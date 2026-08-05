"""The subprocess mechanism the two runners share — and what they don't (R7).

`ctx.execution` (synchronous) and `ctx.jobs` (supervised, backgrounded) are
different things and stay different. Four windows were genuinely the same
implementation and are now one: the timeout kill, the exit/signal decode, the
spool→streams block, and the invocation manifest shape.

The tests here cover the shared helpers directly, including the edge cases
the inline copies handled implicitly, and record the windows deliberately
LEFT duplicated so the reasoning does not have to be rediscovered.
"""

from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import make_store, make_ws

from ctx._proc import exit_status, wait_or_kill

SRC = Path(__file__).resolve().parent.parent / "src" / "ctx"


def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ------------------------------------------------------------ wait_or_kill
def test_a_fast_child_is_not_reported_as_timed_out():
    proc = _spawn("pass")
    assert wait_or_kill(proc, 30) is False
    assert proc.returncode == 0


def test_a_slow_child_times_out_and_is_reaped():
    proc = _spawn("import time; time.sleep(30)")
    t0 = time.monotonic()
    assert wait_or_kill(proc, 0.3) is True
    assert time.monotonic() - t0 < 15
    assert proc.returncode is not None  # reaped, not a zombie
    assert proc.poll() is not None


def test_no_timeout_waits_forever():
    proc = _spawn("import time; time.sleep(0.2)")
    assert wait_or_kill(proc, None) is False
    assert proc.returncode == 0


def test_the_whole_process_group_dies_not_just_the_leader(tmp_path):
    """The reason it is `killpg` and not `proc.kill()`: a shell that forked
    a child leaves an orphan behind if only the leader is signalled. Both
    runners spawn with ``start_new_session``, which is what makes the group
    kill safe to aim at ``proc.pid``."""
    marker = tmp_path / "grandchild.pid"
    script = (
        "import os, subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    proc = _spawn(script)
    for _ in range(200):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    grandchild = int(marker.read_text().strip())
    assert wait_or_kill(proc, 0.3) is True
    for _ in range(200):  # the group kill reaches it, but not instantly
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("the grandchild survived the group kill")


def test_an_already_dead_child_does_not_raise(monkeypatch):
    """`killpg` on a group that is already gone raises ProcessLookupError;
    the fallback to `proc.kill()` is what both copies did."""
    proc = _spawn("import time; time.sleep(30)")

    def boom(*_a, **_k):
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", boom)
    assert wait_or_kill(proc, 0.2) is True
    assert proc.returncode is not None


def test_an_unsignalable_group_does_not_raise(monkeypatch):
    proc = _spawn("import time; time.sleep(30)")

    def boom(*_a, **_k):
        raise PermissionError

    monkeypatch.setattr(os, "killpg", boom)
    assert wait_or_kill(proc, 0.2) is True
    assert proc.returncode is not None


# ------------------------------------------------------------- exit_status
@pytest.mark.parametrize("code", [0, 1, 2, 42, 127, 255])
def test_a_normal_exit_keeps_its_code(code):
    assert exit_status(code) == (code, None)


def test_signal_death_is_not_an_exit_code():
    """The load-bearing property: a run killed by a signal reports
    ``exitCode: null`` plus a name, never a number. `ctx.digest` and the
    reflex plane both branch on ``exitCode is None``."""
    assert exit_status(-signal.SIGKILL) == (None, "SIGKILL")
    assert exit_status(-signal.SIGTERM) == (None, "SIGTERM")
    assert exit_status(-signal.SIGSEGV) == (None, "SIGSEGV")


def test_an_unknown_signal_number_still_gets_a_name():
    """Not an exit code and not a crash — the fallback both copies carried."""
    code, name = exit_status(-999)
    assert code is None and name == "SIG999"


def test_a_still_running_child_reports_none():
    assert exit_status(None) == (None, None)


# --------------------------------------------------- the shared store halves
def test_stream_entries_shape(tmp_path, state_home, workspace_dir):
    from ctx.execution import stream_entries

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    out = tmp_path / "stdout"
    err = tmp_path / "stderr"
    out.write_bytes(b"a\nb\nc")  # no trailing newline
    err.write_bytes(b"")
    got = stream_entries(store, {"stdout": out, "stderr": err})
    assert got["stdout"]["bytes"] == 5
    assert got["stdout"]["lines"] == 3
    assert got["stdout"]["blob"].startswith("sha256:")
    # An empty stream keeps text/plain + utf-8 rather than acquiring a type.
    assert got["stderr"] == {
        "blob": got["stderr"]["blob"],
        "bytes": 0,
        "lines": 0,
        "mediaType": "text/plain",
        "encoding": "utf-8",
    }


def test_stream_entries_detects_binary(tmp_path, state_home, workspace_dir):
    from ctx.execution import stream_entries

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    p = tmp_path / "stdout"
    p.write_bytes(b"\x00\x01\x02binary")
    got = stream_entries(store, {"stdout": p})
    assert got["stdout"]["mediaType"] == "application/octet-stream"


def test_stream_entries_closes_what_it_opens(tmp_path, state_home, workspace_dir):
    """The foreground copy read its 8 KiB head with a bare
    ``path.open("rb").read(...)`` and never closed the handle. Converging on
    the background copy's ``with`` form closed it; a thousand spools must not
    hit the fd limit."""
    from ctx.execution import stream_entries

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    p = tmp_path / "s"
    p.write_bytes(b"x" * 100)
    src = (SRC / "execution.py").read_text(encoding="utf-8")
    assert 'open("rb").read(' not in src
    for _ in range(50):
        stream_entries(store, {"stdout": p})


def test_invocation_manifest_is_the_shape_run_capture_publishes(
    state_home, workspace_dir
):
    from ctx.execution import invocation_manifest, run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    real = run_capture(ws, ["echo", "hi"], store=store).manifest
    synthetic = invocation_manifest(
        ws,
        cwd=real["cwd"],
        argv=real["argv"],
        shell=real["shell"],
        exit_code=real["result"]["exitCode"],
        signal=real["result"]["signal"],
        timed_out=real["result"]["timedOut"],
        streams=real["streams"],
    )
    assert synthetic == {k: v for k, v in real.items() if k != "id"}


def test_foreground_and_background_agree_on_manifest_identity(
    state_home, workspace_dir
):
    """The claim the shared shape exists to keep: identical bytes + argv
    yield an identical manifest id whichever runner produced them."""
    from ctx.execution import run_capture
    from ctx.jobs import finalize_job, start_job, wait_for_done

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    argv = ["sh", "-c", "echo a; echo b >&2; exit 2"]
    fg = run_capture(ws, argv, store=store).manifest
    jid = start_job(ws, store, argv)
    assert wait_for_done(store, jid, timeout=60)
    _digest, bg = finalize_job(ws, store, jid)
    assert fg["streams"] == bg["streams"]
    assert fg["result"] == bg["result"]
    assert fg["argv"] == bg["argv"] and fg["cwd"] == bg["cwd"]


# ------------------------------------------------- what stays duplicated
def test_the_supervisor_stays_dependency_free():
    """`supervise_main` is a detached process that reads everything from
    meta.json. That is why the process helpers live in a stdlib-only module
    instead of in ctx.execution — importing them from there would have
    dragged the store, the workspace resolver and git parsing into it."""
    tree = ast.parse((SRC / "_proc.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert names == ["__future__", "os", "signal", "subprocess"]
    assert not any(n.startswith("ctx") for n in names)


def test_argv_validation_deliberately_stays_separate():
    """The messages match but the exception classes do not, and callers
    catch them by class. A shared validator parameterized by exception type
    is more machinery than the two lines it would save — recorded here so
    the next reader does not "fix" it."""
    from ctx.execution import ExecutionError, run_capture
    from ctx.jobs import JobError, start_job

    assert not issubclass(ExecutionError, JobError)
    assert not issubclass(JobError, ExecutionError)
    assert run_capture.__module__ != start_job.__module__


def test_the_popen_calls_deliberately_stay_separate():
    """Same API, different contracts: one feeds a spooled stdin file and
    raises on FileNotFoundError, the other feeds DEVNULL and records a
    'failed' state transition into meta.json. Superficial similarity."""
    execution = (SRC / "execution.py").read_text(encoding="utf-8")
    jobs = (SRC / "jobs.py").read_text(encoding="utf-8")
    assert "raise ExecutionError(f\"command not found" in execution
    assert 'state="failed", error=f"command not found' in jobs
    assert "stdin_bytes" in execution and "stdin_bytes" not in jobs
