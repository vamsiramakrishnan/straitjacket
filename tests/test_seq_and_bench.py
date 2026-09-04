"""Acceptance: ctx seq (declared command trees, Tura wave), scorecard round
economy + rescue-recovery metrics, and the benchmark manifest guard."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

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
    return resolve_workspace(str(d)), Store("ws_seq_test")


# ------------------------------------------------------------------ ctx seq
def test_seq_green_tree_is_one_terse_round(ws_store):
    from ctx.seq import run_seq

    ws, store = ws_store
    text, code, _timed_out = run_seq(ws, store, ["echo alpha", "echo beta", "echo gamma"])
    assert code == 0
    assert "[ctx seq · 3 steps]" in text
    assert text.count("✓") == 3
    # Every step individually addressable; only the FINAL digest rides.
    assert text.count("run:") >= 3
    assert "--- step 3 digest ---" in text
    assert "--- step 1 digest ---" not in text
    assert "gamma" in text  # final output present (inline path)


def test_seq_halts_at_failure_with_full_evidence(ws_store):
    from ctx.seq import run_seq

    ws, store = ws_store
    text, code, _timed_out = run_seq(
        ws, store,
        ["echo ok", "sh -c 'echo BOOM-DETAIL >&2; exit 7'", "echo never"],
    )
    assert code == 7  # first failure's exit propagates
    assert "halted at step 2 · 1 not run" in text
    assert "step 2 ✗ exit 7" in text
    assert "--- step 2 digest ---" in text
    assert "BOOM-DETAIL" in text  # failure evidence rides in full
    assert "never" not in text  # step 3 did not run


def test_seq_keep_going_runs_all(ws_store):
    from ctx.seq import run_seq

    ws, store = ws_store
    text, code, _timed_out = run_seq(
        ws, store, ["sh -c 'exit 3'", "echo survivor"], halt_on_fail=False
    )
    assert code == 3
    assert "step 2 ✓" in text
    assert "halted" not in text


def test_seq_steps_resolvable_as_runs(ws_store):
    from ctx.retrieval import Selector, get
    from ctx.seq import run_seq

    ws, store = ws_store
    text, _, _timed_out = run_seq(ws, store, ["echo needle-in-step-one", "echo two"])
    rid = next(
        ln.split("run:")[1].split(" ")[0]
        for ln in text.splitlines() if ln.startswith("step 1")
    )
    slice_ = get(store, ws, f"run:{rid}#stdout", Selector(lines=(1, 1)))
    assert "needle-in-step-one" in slice_


def test_cli_seq_exit_codes(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    assert main(["--workspace", str(ws.root), "seq", "echo a", "echo b"]) == 0
    capsys.readouterr()
    assert main(["--workspace", str(ws.root), "seq", "sh -c 'exit 1'"]) == 3


def test_seq_signal_death_is_failure(ws_store):
    """exitCode None from signal death is a failure, not a green step (S6)."""
    from ctx.seq import run_seq

    ws, store = ws_store
    text, code, _timed_out = run_seq(ws, store, ["sh -c 'kill -9 $$'", "echo after"])
    assert code != 0
    assert "step 1 ✗" in text
    assert "after" not in text  # tree halted at the failure


def test_cli_seq_passive_engagement_strips_hints(tmp_path, monkeypatch, capsys):
    """Engagement parity (docs/LADDERS.md edge 1): passive sessions don't
    pay for `next:` suggestion lines on seq digests either."""
    import subprocess

    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text(
        'version = 1\n[engagement]\nmode = "passive"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    from ctx.cli import main

    assert main(
        ["--workspace", str(d), "seq", "sh -c 'yes x | head -5000'"]
    ) == 0
    out = capsys.readouterr().out
    assert "run:" in out  # evidence handle still rides
    assert "next:" not in out  # suggestion lines filtered under passive


# --------------------------------------------- scorecard rounds + recovery
def test_scorecard_rounds_and_rescue_recovery(tmp_path):
    from ctx.scorecard import compute_scorecard, summary_line

    d = tmp_path / "proxy"
    d.mkdir()
    recs = []
    for i in range(1, 7):
        recs.append({
            "seq": i, "path": "/v1/messages", "status": 200,
            "messages": 2 * i, "model": "claude-sonnet-5", "tools": {},
            "usage": {"input_tokens": 2, "output_tokens": 50,
                      "cache_read_input_tokens": 1000 * i,
                      "cache_creation_input_tokens": 100},
            "ms": {"connect": 0.0, "ttfb": 500.0, "total": 900.0},
            **({"rescued": 3} if i >= 4 else {}),
        })
    # plus one single-message side request: counts as request, not round
    recs.append({"seq": 7, "path": "/v1/messages", "status": 200,
                 "messages": 1, "model": "m", "tools": {},
                 "usage": {"input_tokens": 5, "output_tokens": 5}, "ms": {}})
    (d / "wire.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    sc = compute_scorecard(d)
    assert sc["requests"] == 7
    assert sc["rounds"] == 6
    assert sc["rescue_recovery"] == {
        "first_rescued_round": 4, "rounds_after": 2, "blocks_elided": 3,
    }
    line = summary_line(sc)
    assert line.startswith("ctx scorecard: 6 rounds")
    assert "rescue@r4 (+2 rounds after)" in line


# ------------------------------------------------------- benchmark manifest
def test_bench_manifest_matches_frozen_tasks():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
    import matrix_runner as mr

    stored = json.loads(
        (Path(__file__).resolve().parent.parent / "evals" / "bench-manifest.json")
        .read_text(encoding="utf-8")
    )
    current_tasks = {
        k: hashlib.sha256(v[0].encode()).hexdigest()[:16] for k, v in mr.TASKS.items()
    }
    fixtures = hashlib.sha256(
        json.dumps(mr.SURGICAL_SRC, sort_keys=True).encode()
    ).hexdigest()[:16]
    assert stored["tasks"] == current_tasks and stored["fixtures"] == fixtures, (
        "Benchmark task definitions changed. Cross-round comparisons are now "
        "invalid: regenerate evals/bench-manifest.json, and remember the "
        "held-out rule — a mechanism tuned against a task records its win "
        "only on a variant that task never saw (evals/rtk-corpus doc)."
    )


# ---------------------------------------- print-mode background-wait fairness
class _FakeProc:
    def wait(self, timeout=None):
        return 0


def _run_pair_capturing_env(monkeypatch, tmp_path):
    """Run matrix_runner.run_pair with make_fixture and subprocess.Popen faked
    out; returns the list of (argv, env) each arm was launched with."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
    import matrix_runner as mr

    monkeypatch.setattr(
        mr, "make_fixture", lambda scenario, dest, repo: dest.mkdir(parents=True)
    )
    captured = []

    def fake_popen(argv, cwd=None, env=None, stdout=None, stderr=None):
        captured.append((argv, env))
        return _FakeProc()

    monkeypatch.setattr(mr.subprocess, "Popen", fake_popen)
    out = tmp_path / "out"
    out.mkdir()
    mr.run_pair("S6", "sonnet", out, tmp_path / "repo")
    return captured


def test_run_pair_defaults_print_bg_wait_ceiling_for_both_arms(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", raising=False)
    captured = _run_pair_capturing_env(monkeypatch, tmp_path)

    # Both the naive and sj launches must see the same ceiling, or the pair's
    # finding counts would partly measure this timer instead of bug-finding
    # ability (evals/bugbash-round17-2026-09-04.md).
    assert len(captured) == 2
    for _argv, env in captured:
        assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"


def test_run_pair_does_not_inherit_the_parent_session_identity(tmp_path, monkeypatch):
    """Launched from inside a Claude Code session, both arms wrote their
    transcripts under the parent's session id and saw its remote-session
    tools (round 17). Each arm is its own session."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    for _argv, env in _run_pair_capturing_env(monkeypatch, tmp_path):
        assert "CLAUDE_CODE_SESSION_ID" not in env
        assert "CLAUDE_CODE_CHILD_SESSION" not in env


def test_run_pair_leaves_print_bg_wait_ceiling_untouched_if_user_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "5000")
    captured = _run_pair_capturing_env(monkeypatch, tmp_path)

    for _argv, env in captured:
        assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "5000"
