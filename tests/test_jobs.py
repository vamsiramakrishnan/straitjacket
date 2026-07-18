"""Acceptance: long-runner backgrounding (`ctx run --bg`, `ctx job`, `ctx jobs`).

The contract under test: a process that outruns its patience window is
backgrounded under a detached supervisor; its output is only ever a file you
address into. Finalization produces a REAL run manifest — retrieval works on
it identically to a foreground capture — and the digest is byte-identical to
a plain `ctx run` of the same command. Job ids are operational identity only
and never appear in the manifest or the digest body.
"""

import json
import re
import subprocess
import time

import pytest


@pytest.fixture()
def ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def _job_id(out: str) -> str:
    m = re.search(r"job:([0-9a-f]{12})", out)
    assert m, f"no job handle in output:\n{out}"
    return m.group(1)


def _run_short(out: str) -> str:
    m = re.search(r"run:([0-9a-f]{12})", out)
    assert m, f"no run handle in output:\n{out}"
    return m.group(1)


# ------------------------------------------------------------ fast + cheap
def test_bg_after_fast_command_matches_foreground_digest_bytes(ws_store, capsys):
    """(a) A command that finishes inside the patience window finalizes
    inline and emits the exact bytes a plain foreground run emits."""
    from ctx.cli import main

    ws, _ = ws_store
    root = str(ws.root)
    cmd = ["echo", "bg-parity-marker"]
    assert main(["--workspace", root, "run", "--"] + cmd) == 0
    fg = capsys.readouterr().out
    assert "run:" in fg and "bg-parity-marker" in fg
    assert main(["--workspace", root, "run", "--bg-after", "15", "--"] + cmd) == 0
    bg = capsys.readouterr().out
    assert bg == fg  # byte-for-byte, including the run:<id> (same manifest id)


def test_bg_failed_launch_reports_error(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    rc = main(
        ["--workspace", str(ws.root), "run", "--bg-after", "10", "--", "ctx-no-such-cmd-xyz"]
    )
    assert rc == 1
    assert "command not found: ctx-no-such-cmd-xyz" in capsys.readouterr().err


# ------------------------------------------------------------- slow runner
def test_bg_slow_command_backgrounds_then_wait_finalizes(ws_store, capsys):
    """(b) Immediate handle, live partial spool, --wait finalize, retrieval."""
    from ctx.cli import main

    ws, store = ws_store
    root = str(ws.root)
    # Quoting splits the markers so they appear in *output* but never in the
    # command text the status echoes back.
    script = 'echo sta"rted"; sleep 1.5; echo fin"ished"'
    t0 = time.monotonic()
    assert main(["--workspace", root, "run", "--bg", "--shell", "--", script]) == 0
    assert time.monotonic() - t0 < 2.0  # handle returned immediately
    out = capsys.readouterr().out
    jid = _job_id(out)
    assert "backgrounded" in out and "--wait" in out

    time.sleep(0.6)  # let the first echo land in the spool
    assert main(["--workspace", root, "job", jid]) == 0
    status = capsys.readouterr().out
    assert "running" in status
    assert "started" in status  # partial spool is visible...
    assert "finished" not in status  # ...but only what actually ran

    assert main(["--workspace", root, "job", jid, "--wait", "--timeout", "20"]) == 0
    fin = capsys.readouterr().out
    assert f"[ctx job:{jid} finalized → run:" in fin
    assert "finished" in fin
    short = _run_short(fin.split("finalized → ")[1])

    # A later `ctx job <id>` replays the stored digest identically.
    assert main(["--workspace", root, "job", jid]) == 0
    assert capsys.readouterr().out == fin

    # (4) The manifest is a real capture: get/search work like foreground.
    assert main(["--workspace", root, "get", f"run:{short}#stdout", "--lines", "1:2"]) == 0
    got = capsys.readouterr().out
    assert "started" in got and "finished" in got
    assert main(["--workspace", root, "search", f"run:{short}", "finished"]) == 0
    assert "L2: finished" in capsys.readouterr().out


def test_kill_finalizes_partial_output_as_timeout(ws_store, capsys):
    """(c) --kill SIGKILLs the group; what spooled stays addressable under a
    timedOut-style result."""
    from ctx.cli import main

    ws, store = ws_store
    root = str(ws.root)
    assert main(
        ["--workspace", root, "run", "--bg", "--shell", "--", 'echo par"tial"-evidence; sleep 60']
    ) == 0
    jid = _job_id(capsys.readouterr().out)
    time.sleep(0.6)
    assert main(["--workspace", root, "job", jid, "--kill"]) == 0
    out = capsys.readouterr().out
    assert "killed · finalized → run:" in out
    short = _run_short(out.split("finalized → ")[1])

    manifest = store.get_manifest(short)
    assert manifest["result"]["timedOut"] is True
    assert manifest["result"]["exitCode"] is None
    assert manifest["result"]["signal"] == "SIGKILL"
    assert main(["--workspace", root, "get", f"run:{short}#stdout", "--lines", "1:1"]) == 0
    assert "partial-evidence" in capsys.readouterr().out


def test_jobs_lists_workspace_jobs(ws_store, capsys):
    """(d) `ctx jobs` lists ids, coarse state, and the finalized run handle."""
    from ctx.cli import main

    ws, _ = ws_store
    root = str(ws.root)
    assert main(["--workspace", root, "run", "--bg-after", "15", "--", "echo", "listed"]) == 0
    capsys.readouterr()
    assert main(["--workspace", root, "run", "--bg", "--shell", "--", "sleep 15"]) == 0
    running_jid = _job_id(capsys.readouterr().out)
    from ctx.jobs import job_state

    _, store = ws_store[0], ws_store[1]
    deadline = time.monotonic() + 10
    while job_state(store, running_jid) == "launching" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert main(["--workspace", root, "jobs"]) == 0
    listing = capsys.readouterr().out
    assert "[ctx jobs · 2]" in listing
    assert "finalized → run:" in listing
    assert f"job:{running_jid}" in listing and "running" in listing
    # cleanup: don't leak the sleeper past the test
    assert main(["--workspace", root, "job", running_jid, "--kill"]) == 0
    capsys.readouterr()


def test_job_id_never_enters_manifest_or_digest(ws_store, capsys):
    """(e) Operational identity stays operational: the job id appears in
    neither the finalized manifest bytes nor the stored digest body."""
    from ctx.cli import main
    from ctx.store import canonical_json

    ws, store = ws_store
    root = str(ws.root)
    assert main(
        ["--workspace", root, "run", "--bg", "--shell", "--", 'echo id"-hygiene"']
    ) == 0
    jid = _job_id(capsys.readouterr().out)
    assert main(["--workspace", root, "job", jid, "--wait", "--timeout", "20"]) == 0
    short = _run_short(capsys.readouterr().out)

    manifest = store.get_manifest(short)
    assert jid not in canonical_json(manifest).decode("utf-8")
    jobdir = store.audit_dir / "jobs" / jid
    digest = (jobdir / "digest").read_text(encoding="utf-8")
    assert jid not in digest
    # No pids or timestamps either: the manifest carries only declared keys.
    assert set(manifest) == {
        "schema", "workspaceId", "cwd", "argv", "shell",
        "result", "streams", "source", "digest", "id",
    }


def test_finalization_is_deterministic_across_repeats(ws_store):
    """Identical bytes + argv ⇒ identical manifest id, foreground or bg."""
    from ctx.jobs import finalize_job, start_job, wait_for_done

    ws, store = ws_store
    ids = set()
    for _ in range(2):
        jid = start_job(ws, store, ["echo", "det-check"])
        assert wait_for_done(store, jid, timeout=15)
        digest, manifest = finalize_job(ws, store, jid)
        ids.add(manifest["id"])
        assert digest.startswith("[ctx run:")
    assert len(ids) == 1


def test_status_spool_view_is_bounded(ws_store, capsys):
    """A flooding job's live status stays bounded: ≤ ~40 spool lines shown,
    long lines clipped, omission declared."""
    from ctx.cli import main

    ws, _ = ws_store
    root = str(ws.root)
    flood = "for i in $(seq 1 5000); do echo line-$i-" + "x" * 400 + "; done; sleep 15"
    assert main(["--workspace", root, "run", "--bg", "--shell", "--", flood]) == 0
    jid = _job_id(capsys.readouterr().out)
    time.sleep(0.8)
    assert main(["--workspace", root, "job", jid]) == 0
    status = capsys.readouterr().out
    lines = status.splitlines()
    assert len(lines) < 55  # ~40 spool lines + framing
    assert all(len(ln) < 260 for ln in lines)  # clipped
    assert "omitted" in status  # declared omission, never silent
    assert main(["--workspace", root, "job", jid, "--kill"]) == 0
    capsys.readouterr()
