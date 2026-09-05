"""Eleven defects from bug-bash round 17's HARNESSED arm (11/11 confirmed).

The re-run of the harnessed S6 arm on the fixed tree (single-shot notice in
place) kept its turn and completed all eight subagents; it then hit the
30-turn cap while collecting their output, so its main agent never compiled
a ranked report. The eight subagent reports were read directly
(evals/bugbash-round17-2026-09-04.md). Every finding below was reproduced
against the tree before its fix. None of them was in the naive arm's list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from conftest import make_store, make_ws


# ------------------------------------------ 1. rollback leaks its own temp file
def test_failed_rollback_rename_does_not_leave_a_temp_file(state_home, workspace_dir, monkeypatch):
    """_stage() unlinks its temp file only on its OWN failure; when the
    rollback's os.replace failed, the staged restore file leaked."""
    import ctx.edit_transactions as tx
    from ctx import anchors
    from ctx.edit_transactions import REQUEST_SCHEMA, EditTransactionError

    a = workspace_dir / "a.py"
    b = workspace_dir / "b.py"
    a.write_text("a = 1\n", encoding="utf-8")
    b.write_text("b = 1\n", encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)

    def edit(path, line, new):
        return {"path": path, "span": anchors.format_span(1, 1, anchors.anchor([line])),
                "replacement": new}

    plan = tx.create_edit_plan(ws, store, {"schema": REQUEST_SCHEMA,
                                           "edits": [edit("a.py", "a = 1", "a = 2\n"),
                                                     edit("b.py", "b = 1", "b = 2\n")]})
    real = tx.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] >= 2:          # b.py's commit, then a.py's restore
            raise OSError("simulated rename failure")
        return real(src, dst)

    monkeypatch.setattr(tx.os, "replace", flaky)
    with pytest.raises(EditTransactionError) as caught:
        tx.apply_edit_plan(ws, plan)
    assert "rollback could not safely restore" in str(caught.value)
    assert not list(workspace_dir.glob(".ctx-edit-*")), "staged restore file leaked"


# ------------------------------------------- 2. hiding a hidden family is a no-op
def test_hiding_an_unrevealed_family_reports_no_change(tmp_path):
    from test_surface_gateway import _FAKE

    from ctx import surface_gateway as gw

    root = tmp_path / "proj"
    root.mkdir()
    (root / "fake_mcp.py").write_text(_FAKE, encoding="utf-8")
    (root / "ctx.toml").write_text("version=1\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": sys.executable, "args": [str(root / "fake_mcp.py")]}}}))
    g = gw.Gateway(root)
    try:
        text, changed = g.call("surface_hide", {"family": "remote-source-control"})
        assert changed is False and "already hidden" in json.dumps(text)
        _, changed = g.call("surface_reveal", {"family": "remote-source-control"})
        assert changed is True
        _, changed = g.call("surface_hide", {"family": "remote-source-control"})
        assert changed is True
    finally:
        g.close()


# ---------------------------------- 3. ctx get numbers lines the way the index does
def test_get_splits_lines_like_the_line_index(state_home, workspace_dir, capsys):
    """U+2028 inside a JS string literal is one line to the index and to
    ripgrep; splitlines() made it two, so the header said 3 and the body
    printed 4 with every number after it off by one."""
    from ctx.cli import main

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob('line1\nvar s = "a b";\nline3\n'.encode("utf-8"))
    assert main(["--workspace", str(workspace_dir), "get", f"blob:{blob}", "--lines", "1:3"]) == 0
    out = capsys.readouterr().out
    assert "of 3" in out
    assert out.count("\nL") == 3 and "L4:" not in out
    assert 'var s = "a b";' in out


def test_index_lines_contract():
    from ctx.textutil import index_lines

    assert index_lines("") == []
    assert index_lines("a\nb\n") == ["a", "b"]
    assert index_lines("a\r\nb") == ["a", "b"]
    assert index_lines("a b\n\x0cc") == ["a b", "\x0cc"]


# ---------------------------------------------- 4. an empty stream is an answer
def test_get_returns_an_empty_blob_instead_of_refusing(state_home, workspace_dir, capsys):
    from ctx.cli import main

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(b"")
    assert main(["--workspace", str(workspace_dir), "get", f"blob:{blob}"]) == 0
    assert "(empty)" in capsys.readouterr().out
    # An explicit range on nothing is still refused, and the message no
    # longer suggests a range that would refuse again.
    rc = main(["--workspace", str(workspace_dir), "get", f"blob:{blob}", "--lines", "1:1"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "empty" in err and "1:1" not in err.split("selects nothing", 1)[1]


# ------------------------------------------------ 5. doctor closes its stores
def test_doctor_closes_every_store_it_opens(state_home, workspace_dir, monkeypatch):
    from ctx import installer, store as store_mod

    ws = make_ws(workspace_dir)
    opened, closed = [], []
    real_init, real_close = store_mod.Store.__init__, store_mod.Store.close

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        opened.append(self)

    def close(self):
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(store_mod.Store, "__init__", init)
    monkeypatch.setattr(store_mod.Store, "close", close)
    installer.doctor_checks(ws)
    assert opened and len(closed) >= len(opened)


# ------------------------------------- 6. "." as a target fails the contract way
@pytest.mark.parametrize("target", [".", "./", "./."])
def test_whole_workspace_target_is_refused_with_the_module_error(target):
    from ctx.worktree_isolation import WorktreeIsolationError, normalize_targets

    with pytest.raises(WorktreeIsolationError, match="whole repository"):
        normalize_targets((target,))


# ------------------------------------ 7. reflex state survives parallel hooks
def test_reflex_mutators_do_not_lose_updates_across_processes(state_home, workspace_dir):
    """Two hooks from one assistant turn (parallel tool calls) both read
    seq=5 and both wrote 6. Under the lock, N processes x K emissions land
    exactly N*K."""
    from ctx import reflex

    ws = make_ws(workspace_dir)
    root = str(ws.root)
    reflex.emit_intervention(root, signature="pytest -q", generation="g0")  # create the ledger dir
    n, k = 6, 8
    pids = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            try:
                for _ in range(k):
                    reflex.emit_intervention(root, signature="pytest -q", generation="g0")
            finally:
                os._exit(0)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    assert reflex.read_state(root)["seq"] == 1 + n * k


# ------------------------------------------- 8. the symbol list keeps the newest
def test_symbol_grep_count_keeps_growing_past_64(state_home, workspace_dir):
    from ctx.engagement import note_symbol_grep

    ws = make_ws(workspace_dir)
    for i in range(70):
        count, _ = note_symbol_grep(ws.root, f"sym{i}")
    assert count == 64
    from ctx import engagement

    state = engagement._mutate_state(ws.root, lambda s: s)
    assert "sym69" in state["grep_symbols"] and "sym0" not in state["grep_symbols"]


# ------------------------------ 9. a timed-out host launch kills what it forked
def test_launch_timeout_kills_the_hosts_grandchildren(tmp_path):
    from ctx import hosts, orchestrator

    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "fakehost"
    script.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pidfile}\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    codex = next(h for h in hosts.detect_all(which=lambda b: str(script)) if h.spec.name == "codex")
    t0 = time.monotonic()
    code, _out, err, _usage = orchestrator._launch_host(codex, tmp_path, "p", "ctx", timeout=1.0)
    assert code == 127 and "TimeoutExpired" in err
    assert time.monotonic() - t0 < 10
    grandchild = int(pidfile.read_text().strip())
    def gone() -> bool:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return True
        # A killed-but-unreaped child of a dead shell lingers as a zombie;
        # its /proc entry can vanish between the check and the read.
        try:
            with open(f"/proc/{grandchild}/stat") as fh:
                return fh.read().split(")")[-1].split()[0] == "Z"
        except OSError:
            return True

    for _ in range(50):  # SIGKILL delivery is asynchronous
        if gone():
            break
        time.sleep(0.1)
    else:
        os.kill(grandchild, 9)
        pytest.fail("grandchild survived the host's timeout")


# --------------------------- 10. a supervisor that dies before running is noticed
def test_job_stuck_in_launching_with_a_dead_supervisor_is_failed(state_home, workspace_dir):
    from ctx import jobs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    jobdir = jobs._job_dir(store, "deadbeefdead")
    jobdir.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    jobs._write_meta(jobdir, {"schema": "ctx.job/v1", "argv": ["x"], "state": "launching",
                              "createdAt": time.time(), "launcherSupervisorPid": dead.pid})
    assert jobs.job_state(store, "deadbeefdead") == "failed"
    with pytest.raises(jobs.JobError, match="supervisor exited"):
        jobs.wait_for_done(store, "deadbeefdead", timeout=1)


def test_start_job_records_the_supervisor_pid(state_home, workspace_dir):
    from ctx import jobs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    job_id = jobs.start_job(ws, store, ["true"])
    meta = jobs._read_meta(jobs._job_dir(store, job_id))
    assert meta.get("launcherSupervisorPid") or meta.get("state") in ("running", "done")
    jobs.wait_for_done(store, job_id, timeout=20)


# ------------------------------- 11. a finished job's exit code reaches the caller
def test_bare_job_query_reports_a_failed_background_command(state_home, workspace_dir, capsys):
    from test_jobs import _job_id

    from ctx import jobs
    from ctx.cli import main

    root = str(workspace_dir)
    assert main(["--workspace", root, "run", "--bg", "--", "false"]) == 0
    jid = _job_id(capsys.readouterr().out)
    store = make_store(make_ws(workspace_dir))
    assert jobs.wait_for_done(store, jid, timeout=20)
    assert main(["--workspace", root, "job", jid]) == 3
    capsys.readouterr()
