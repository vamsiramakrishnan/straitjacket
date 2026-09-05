"""Ten defects from bug-bash round 17 (10/10 confirmed by hand, precision 1.0).

Round 17 ran the S6 cell live as a naive-vs-harnessed pair on the same model
(evals/bugbash-round17-2026-09-04.md). The naive arm delivered nine ranked
findings; the harnessed arm fanned out into seven subagents that print mode
killed at its background ceiling before any reported, so its only
contribution is one independent rediscovery (the `lean_models` crash) read
out of a killed subagent's transcript. Every finding below was reproduced
against the tree before its fix landed; none was taken on the report's word.

Two are old lessons through new doors. The redirect shortcut had already
been taught not to return ahead of the repo's deny list (round S6, 2026-07)
and still returned ahead of the secret-path guard; `lean_models` was the one
list field left iterating raw TOML after `deny_commands = 42` taught this
file to coerce (round 15).
"""

from __future__ import annotations

import json
import sys
import threading

import pytest

from conftest import make_store, make_ws


# ------------------------------------------ 1. redirect shortcut, secret path
@pytest.mark.parametrize("cmd", [
    "cat .env > /tmp/out.log 2>&1",
    "head id_rsa > /tmp/o 2>&1",
    "cat secrets.json &> /tmp/o3",
])
def test_redirected_secret_read_still_force_asks(cmd):
    """`cat .env` force-asks; `cat .env > out.log 2>&1` was allowed. The
    shortcut answers the volume question only."""
    from ctx.hook import _load_guard_policy, classify_command

    pol = _load_guard_policy(None)
    assert classify_command("cat .env", pol)["decision"] == "force_ask"
    assert classify_command(cmd, pol)["decision"] == "force_ask"


def test_redirect_shortcut_still_answers_the_volume_question():
    from ctx.hook import _load_guard_policy, classify_command

    pol = _load_guard_policy(None)
    assert classify_command("pytest > out.log 2>&1", pol)["decision"] == "allow"


# ------------------------------------------------- 2. lean_models coercion
@pytest.mark.parametrize("raw", ["42", "true", '"sonnet"', "3.5"])
def test_non_list_lean_models_keeps_the_default(tmp_path, raw):
    """`lean_models = 42` raised TypeError out of load_config, on every
    command's path; `"sonnet"` became ('s','o','n','n','e','t')."""
    from ctx.config import Engagement, load_config

    (tmp_path / "ctx.toml").write_text(
        f"version = 1\n[engagement]\nlean_models = {raw}\n", encoding="utf-8"
    )
    assert load_config(tmp_path).engagement.lean_models == Engagement().lean_models


def test_list_lean_models_still_loads(tmp_path):
    from ctx.config import load_config

    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[engagement]\nlean_models = ["a", "b"]\n', encoding="utf-8"
    )
    assert load_config(tmp_path).engagement.lean_models == ("a", "b")


# ------------------------------------------ 3. digest_output records is_error
def test_errored_tool_result_digests_as_a_failure(state_home, workspace_dir):
    """The manifest hard-coded exitCode 0; the digest for a failed build's
    stack trace read "exit 0" and the stored run remembered a success."""
    from ctx.digest import digest_output

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    body = "Traceback (most recent call last):\n" + "  boom\n" * 400
    text_err, short_err = digest_output(store, ws, "Bash", body, "", is_error=True)
    text_ok, short_ok = digest_output(store, ws, "Bash", body, "", is_error=False)
    assert "exit 1" in text_err and "exit 0" not in text_err
    assert "exit 0" in text_ok
    assert store.get_manifest(short_err)["result"]["exitCode"] == 1
    assert store.get_manifest(short_ok)["result"]["exitCode"] == 0
    # Different facts, different ids: identity is (bytes, tool, is_error).
    assert short_err != short_ok


# --------------------------------------------- 4. narrowed epochs ladder
def test_epoch_rung_survives_a_narrowed_ladder():
    """`[rungs[2]] * 0` still evaluates rungs[2]; a two-rung `[ladders.epochs]`
    (documented, validated) crashed `ctx ladders` with IndexError."""
    from ctx.ladders import _epoch_rung

    two = ("unknown", "promoted")
    assert _epoch_rung({"policy": {}}, two) == ["unknown"]
    assert _epoch_rung({"policy": {"promoted_commands": ["a", "b"]}}, two) == ["promoted"] * 2
    assert _epoch_rung({"policy": {"demoted_commands": ["c"]}}, two) == ["unknown"]
    three = ("unknown", "promoted", "demoted")
    assert _epoch_rung({"policy": {"promoted_commands": ["a"], "demoted_commands": ["c"]}},
                       three) == ["promoted", "demoted"]
    assert _epoch_rung({}, ()) == []


# -------------------------------------------- 5. builtin ranker, unread file
def test_builtin_rank_skips_edges_to_files_it_never_read():
    """The module index is built from the listing, the file table from what
    could be read; an import of a listed-but-unreadable module was a KeyError
    in the ranker every install without networkx uses."""
    from ctx.repomap import _FileMap, _rank

    files = {"a.py": _FileMap(rel="a.py"), "b.py": _FileMap(rel="b.py")}
    _rank(files, {"a.py": {"b.py", "unreadable.py"}, "b.py": set()})
    assert files["b.py"].imported_by == 1
    assert files["a.py"].imported_by == 0
    assert files["b.py"].score > files["a.py"].score


# -------------------------------------- 6. gateway deadline vs a partial line
_PARTIAL = (
    "import sys,json,time\n"
    "for line in sys.stdin:\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize':\n"
    "  print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'h','version':'1'}}}),flush=True)\n"
    " elif meth=='tools/list':\n"
    "  sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":%d,\"resu' % mid); sys.stdout.flush(); time.sleep(60)\n"
)


def test_backend_partial_line_cannot_outlive_the_deadline(tmp_path):
    """select() proved a byte was readable; readline() then waited for the
    newline with no timeout at all -- the hung-backend case the deadline
    exists for. Run under a thread so the old code fails instead of hanging."""
    from ctx import surface_gateway as gw

    script = tmp_path / "partial.py"
    script.write_text(_PARTIAL, encoding="utf-8")
    b = gw.MCPBackend("h", [sys.executable, str(script)], timeout=1.0)
    assert b._ensure() is not None
    box: dict = {}

    def call():
        box["resp"] = b._rpc("tools/list", {})

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(8.0)
    try:
        assert not t.is_alive(), "_rpc blocked past its deadline on a partial line"
        assert box["resp"] is None
    finally:
        b.close()


# --------------------------------------- 7. failing ids decided per line
def test_failing_ids_are_decided_per_line_not_per_transcript():
    """One FAILED among passes tagged every id as failing, because the filter
    tested the whole result and never the match."""
    from ctx.evidence_outcomes import _failing_ids

    verbose = (
        "tests/test_a.py::test_one PASSED\n"
        "tests/test_a.py::test_two FAILED\n"
        "tests/test_a.py::test_three PASSED\n"
        "tests/test_a.py::test_four XFAILED\n"
        "2 passed, 1 failed, 1 xfailed\n"
    )
    assert _failing_ids(verbose) == frozenset({"tests/test_a.py::test_two"})
    # The digest's numbered failure list carries no verdict per line.
    digest = "3 failed\n1. tests/test_a.py::test_two\n2. tests/test_b.py::test_x\n"
    assert _failing_ids(digest) == frozenset({"tests/test_a.py::test_two", "tests/test_b.py::test_x"})
    # A bare id with no verdict anywhere is not a failure.
    assert _failing_ids("tests/test_a.py::test_one\ntests/test_a.py::test_two\n") == frozenset()


def test_mixed_transcript_keeps_passing_ids_out_of_failing_ids():
    from ctx.evidence_outcomes import emissions_from_calls

    calls = [{
        "tool": "Bash", "input": {"command": "pytest -v"},
        "result": "tests/test_a.py::test_one PASSED\ntests/test_a.py::test_two FAILED\n1 failed",
    }]
    (em,) = emissions_from_calls(calls)
    assert em.failing_ids == frozenset({"tests/test_a.py::test_two"})
    assert "tests/test_a.py::test_one" in em.test_ids


# -------------------------------------------- 8. scorecard: collapse to zero
def test_full_cache_collapse_counts_as_an_invalidation(tmp_path):
    """cache_read 5000 -> 0 on an established thread is the largest possible
    invalidation and was the one the counter skipped (`u_read and ...`)."""
    from test_scorecard import _wire_record

    from ctx.scorecard import compute_scorecard

    d = tmp_path / "proxy"
    d.mkdir()
    records = [
        _wire_record(1, msgs=2, cre=5_000, read=0),      # cold prefix
        _wire_record(2, msgs=3, cre=200, read=5_000),    # thread established
        _wire_record(3, msgs=4, cre=6_000, read=0),      # full rewrite
    ]
    (d / "wire.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    assert compute_scorecard(d)["invalidations"] == 1


# ------------------------------------ 9. a dead run pointer is no run at all
def test_fails_sites_does_not_serve_a_collected_run(state_home, workspace_dir, monkeypatch):
    """gc prunes manifests, not facts.sqlite; the cached `latest_run` pointer
    then served the old census with a `run:` citation that no longer
    resolved. The pointer is honoured only while its run still exists."""
    from ctx import facts

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    stale = [{"file": "t.py", "line": 1, "test": "t::x", "failure_class": "assert", "text": "x"}]
    monkeypatch.setattr(facts, "_newest_captured", lambda s: None)
    monkeypatch.setattr(facts, "_meta_get", lambda conn, key: "deadbeefdead")
    monkeypatch.setattr(facts, "_fails_for", lambda s, rid: stale if rid == "deadbeefdead" else [])
    monkeypatch.setattr(facts, "_derive_newest", lambda s, w: {"ok": False})
    assert facts.fails_sites(ws, store) == []          # the run is gone
    monkeypatch.setattr(facts, "_run_exists", lambda s, rid: True)
    assert facts.fails_sites(ws, store) == stale       # the run is still there


# --------------------------------------------- 10. rg honours follow_symlinks
def test_rg_engine_passes_follow_through(workspace_dir, state_home, monkeypatch):
    """`follow_symlinks = true` was a no-op on the engine most installs use."""
    import subprocess

    from ctx._retrieval import rg_engine

    (workspace_dir / "ctx.toml").write_text(
        "version = 1\n[workspace]\nfollow_symlinks = true\n", encoding="utf-8"
    )
    ws = make_ws(workspace_dir)
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(rg_engine.subprocess, "run", fake_run) if hasattr(rg_engine, "subprocess") else None
    monkeypatch.setattr(subprocess, "run", fake_run)
    rg_engine._rg_repo_search(ws, [], ["needle"], [__import__("re").compile("needle")],
                              fixed=True, glob=None)
    assert "--follow" in seen["argv"]
