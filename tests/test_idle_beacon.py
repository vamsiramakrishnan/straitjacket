"""The orchestrator's inactivity bound, beside its wall clock.

headlong's shellm keeps two bounds apart: a run that goes silent for
SHELLM_INACTIVITY_TIMEOUT is dead, a run still emitting is allowed up to
its wall clock. Before this, `node_timeout` was the only bound, and a node
killed by it was filed by the steward as a transport blip. Pinned here:
every byte on either stream is a beacon; silence for `idle_timeout` kills
the process group and raises `NodeStalled`; a chatty node is still bounded
by the wall clock; the launcher reports the two differently; the bound is
opt-in and never reaches an injected launcher's signature.
"""
import os
import subprocess
import sys
import time

import pytest

from ctx import hosts, orchestrator
from ctx.config import OrchestratePolicy
from ctx.orchestrator import NodeStalled, _launch_host, _run_bounded

PY = sys.executable


def _script(tmp_path, body: str):
    p = tmp_path / "host.py"
    p.write_text(body, encoding="utf-8")
    return [PY, str(p)]


def test_the_bound_is_opt_in():
    assert OrchestratePolicy().idle_timeout == 0.0


def test_a_silent_node_is_killed_for_inactivity_with_its_grandchildren(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    argv = _script(tmp_path, f"""
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(str(p.pid))
print("started", flush=True)
time.sleep(60)
""")
    t0 = time.monotonic()
    with pytest.raises(NodeStalled) as info:
        _run_bounded(argv, cwd=tmp_path, timeout=30.0, env=dict(os.environ), idle_timeout=0.6)
    elapsed = time.monotonic() - t0
    assert elapsed < 10, elapsed
    assert "no output for 1s" in str(info.value) and "wall clock" in str(info.value)
    assert info.value.output.startswith("started")  # what it did say is kept
    grandchild = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        try:
            with open(f"/proc/{grandchild}/stat") as fh:
                if fh.read().split(")")[-1].split()[0] == "Z":
                    break
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(grandchild, 9)
        pytest.fail("grandchild survived the stall kill")


def test_a_chatty_node_is_never_stalled_but_still_hits_the_wall(tmp_path):
    argv = _script(tmp_path, """
import sys, time
for i in range(12):
    sys.stderr.write(".")   # progress without a newline, on stderr
    sys.stderr.flush()
    time.sleep(0.15)
print("finished")
""")
    done = _run_bounded(argv, cwd=tmp_path, timeout=30.0, env=dict(os.environ), idle_timeout=0.6)
    assert done.returncode == 0 and done.stdout.strip() == "finished" and done.stderr == "." * 12

    with pytest.raises(subprocess.TimeoutExpired) as info:
        _run_bounded(argv, cwd=tmp_path, timeout=0.7, env=dict(os.environ), idle_timeout=0.6)
    assert not isinstance(info.value, NodeStalled)


def test_the_launcher_reports_a_stall_by_name(tmp_path):
    script = tmp_path / "fakehost"
    script.write_text("#!/bin/sh\necho started\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    codex = next(h for h in hosts.detect_all(which=lambda b: str(script)) if h.spec.name == "codex")
    code, _out, err, _usage = _launch_host(codex, tmp_path, "p", "ctx", timeout=30.0, idle_timeout=0.6)
    assert code == 127 and err.startswith("NodeStalled:")
    # the steward reads that name, not the "timed out" wording of a wall kill
    from ctx.steward import classify_failure
    cls = classify_failure(code=code, stdout="", stderr=err, turns=0, attempt=1,
                           expected_turns=12, contract_failed=True)
    assert (cls.reason, cls.failure_kind) == ("failed", "stalled")


def test_claude_streams_its_events_only_when_the_bound_is_on(monkeypatch, tmp_path):
    seen = {}

    class Completed:
        returncode, stdout, stderr = 0, "{}", ""

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return Completed()

    monkeypatch.setattr(orchestrator, "_run_bounded", fake_run)
    claude = next(h for h in hosts.detect_all(which=lambda b: f"/usr/bin/{b}") if h.spec.name == "claude")
    _launch_host(claude, tmp_path, "do it", "/usr/bin/ctx", timeout=5)
    assert "json" in seen["argv"] and "stream-json" not in seen["argv"]
    assert "idle_timeout" not in seen["kwargs"]  # default launch: byte-identical call
    _launch_host(claude, tmp_path, "do it", "/usr/bin/ctx", timeout=5, idle_timeout=120)
    i = seen["argv"].index("--output-format")
    assert seen["argv"][i + 1 : i + 3] == ["stream-json", "--verbose"]
    assert seen["kwargs"]["idle_timeout"] == 120


def test_the_bound_never_reaches_an_injected_launcher(state_home, git_workspace, monkeypatch):
    """Tests and evals inject launchers with the historical signature; the
    idle bound is passed only to the real one, like the turn ceiling."""
    from dataclasses import replace

    from conftest import make_ws
    from ctx.orchestrator import build_route_plan, run_route

    ws = make_ws(git_workspace)
    H = [h for h in hosts.detect_all(which=lambda b: "/usr/bin/claude" if b == "claude" else None)
         if h.installed and h.harnessable]
    calls = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        calls.append((host.name, timeout))
        return 0, "done at repo:x.py:1", ""

    cfg = replace(ws.config.orchestrate, idle_timeout=45.0, node_timeout=123.0)
    raw = {"nodes": [{"id": "explore", "goal": "look around", "min_tier": "economy", "deps": []}]}
    plan = build_route_plan("t", raw, H, cfg)
    result = run_route(ws, plan, cfg, launch=launch)
    assert calls == [("claude", 123.0)] and result.outcomes[0].status == "ok"
