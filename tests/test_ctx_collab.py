from __future__ import annotations

import json
import subprocess

import pytest

from ctx.collab import (
    CollabError,
    Ledger,
    handoff_packet,
    ledger_for_workspace,
    spawn,
)
from ctx.workspace import resolve_workspace


@pytest.fixture()
def ledger(tmp_path):
    return Ledger(tmp_path / ".ctx" / "collab")


def _make_task(ledger):
    return ledger.create_task(
        "Implement parser tests",
        owns=("tests/parser/",),
        avoid=("src/parser/",),
        verify="pytest tests/parser",
        context=("docs/PLAN.md",),
        created_by="coordinator",
    )


def test_task_claim_complete_review_integration_lifecycle(ledger):
    tid = _make_task(ledger)
    state = ledger.state()[tid]
    assert state["status"] == "open" and state["holder"] is None

    ledger.claim(tid, "codex")
    state = ledger.state()[tid]
    assert state["holder"] == "codex" and state["status"] == "claimed"

    # A second harness cannot steal an active lease.
    with pytest.raises(CollabError, match="leased to codex"):
        ledger.claim(tid, "gemini")
    # The holder may re-assert its own lease idempotently.
    ledger.claim(tid, "codex")

    ledger.complete(
        tid,
        "codex",
        status="done",
        summary="added 9 parser tests",
        files_changed=("tests/parser/test_edges.py",),
        verification="pytest tests/parser -> 9 passed",
        followups=("review by second harness",),
    )
    state = ledger.state()[tid]
    assert state["status"] == "done"
    assert state["last_completion"]["files_changed"] == ["tests/parser/test_edges.py"]

    ledger.review(tid, "gemini", verdict="approve", evidence="tests are real and pass")
    ledger.verify(tid, ledger.dir.parent.parent,
                  runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="9 passed", stderr=""))
    ledger.integrate(tid, accepted=True, note="merged after review")
    assert ledger.state()[tid]["status"] == "integrated"

    # Integrated tasks are closed to new claims.
    with pytest.raises(CollabError, match="already integrated"):
        ledger.claim(tid, "agy")


def test_blocked_completion_releases_the_lease(ledger):
    tid = _make_task(ledger)
    ledger.claim(tid, "claude")
    ledger.complete(tid, "claude", status="blocked", summary="missing fixture")
    assert ledger.state()[tid]["holder"] is None


def test_ledger_survives_a_torn_final_write(tmp_path):
    ledger = Ledger(tmp_path / "c")
    tid = ledger.create_task("goal one")
    path = ledger.path
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema": "ctx.collab/v1", "kind": "note"')  # torn line
    rows = ledger.load()
    assert len(rows) >= 1
    assert all(r.get("kind") for r in rows)


def test_handoff_packet_carries_leases_and_verification(ledger):
    tid = _make_task(ledger)
    packet = handoff_packet(ledger.state()[tid], "gemini")
    assert "# Handoff " in packet and "-> gemini" in packet
    assert "tests/parser/" in packet
    assert "Do NOT touch: src/parser/" in packet
    assert "pytest tests/parser" in packet
    assert "never revert other harnesses' work" in packet
    assert "completion packet" in packet


def test_spawn_builds_documented_headless_commands():
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    for harness, prefix in {
        "claude": ("claude", "-p"),
        "codex": ("codex", "exec"),
        "gemini": ("gemini", "-p"),
        "agy": ("agy", "-p"),
    }.items():
        spawn(harness, "hello", runner=fake_run)
        assert tuple(seen["argv"][:2]) == prefix
        assert seen["argv"][-1] == "hello"

    with pytest.raises(CollabError, match="unknown harness"):
        spawn("gpt", "hi", runner=fake_run)


def test_workspace_ledger_lives_under_ctx_collab(tmp_path):
    (tmp_path / ".git").mkdir()
    ws = resolve_workspace(str(tmp_path))
    ledger = ledger_for_workspace(ws)
    assert str(ledger.path).replace("\\", "/").endswith(".ctx/collab/ledger.jsonl")


def test_cli_collab_round_trip(tmp_path, capsys):
    from ctx.cli import main

    (tmp_path / ".git").mkdir()
    ws_args = ["--workspace", str(tmp_path)]

    assert main([*ws_args, "collab", "new", "--goal", "fix flaky test",
                 "--owns", "tests/test_flaky.py", "--verify", "pytest -k flaky"]) == 0
    task_id = capsys.readouterr().out.strip().splitlines()[-1]

    assert main([*ws_args, "collab", "claim", task_id, "--by", "codex"]) == 0
    assert main([*ws_args, "collab", "complete", task_id, "--by", "codex",
                 "--status", "done", "--summary", "fixed",
                 "--verification", "pytest -k flaky -> 1 passed"]) == 0
    assert main([*ws_args, "collab", "list"]) == 0
    listed = capsys.readouterr().out
    assert f"{task_id} [done]" in listed

    # Lease conflict surfaces as a clean error, not a traceback.
    assert main([*ws_args, "collab", "claim", task_id, "--by", "gemini"]) == 0
    ledger = Ledger(tmp_path / ".ctx" / "collab")
    assert ledger.state()[task_id]["holder"] == "gemini"

    events = [json.loads(line) for line in
              (tmp_path / ".ctx" / "collab" / "ledger.jsonl").read_text().splitlines()]
    kinds = [e["kind"] for e in events]
    assert kinds.count("task") == 1 and "claim" in kinds and "completion" in kinds
