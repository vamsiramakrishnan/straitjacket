"""Acceptance: ``ctx ask`` — intents as typed plan presets (docs/ASK.md,
M-L). Two layers: the pure compiler (deterministic slots → ctx.plan/v1,
teaching errors that suggest but never guess-and-run) and the thin ops
(evidence.failures never reruns and declares freshness; code.symbols;
code.context is terminal). One end-to-end diagnose proves the no-rerun
invariant on a real captured failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import make_store, make_ws

VENV_PY = sys.executable  # the interpreter running the suite carries pytest


# ------------------------------------------------------ the pure compiler
def test_compile_locate_slots_are_deterministic():
    from ctx import ask

    a, disc_a = ask.compile_ask("locate", "Where is AuthContext used", symbol="AuthContext")
    b, _ = ask.compile_ask("locate", "totally different phrasing", symbol="AuthContext")
    # Same slots ⇒ byte-identical plan (question aside) ⇒ stable node-cache keys.
    pa, pb = json.loads(a), json.loads(b)
    assert [s["op"] for s in pa["steps"]] == ["code.refs", "code.symbols",
                                              "evidence.group", "code.context"]
    pa["objective"]["question"] = pb["objective"]["question"] = "x"
    assert json.dumps(pa, sort_keys=True) == json.dumps(pb, sort_keys=True)
    assert any("subject: AuthContext" in d for d in disc_a)


def test_compile_diagnose_reads_failures_never_reruns():
    from ctx import ask

    plan_json, _ = ask.compile_ask("diagnose", "Why are tests failing")
    ops = [s["op"] for s in json.loads(plan_json)["steps"]]
    assert "evidence.failures" in ops
    assert "test.run" not in ops  # the no-rerun invariant, at compile time
    assert "evidence.join" in ops  # counterevidence is structural


def test_locate_and_impact_emit_coverage_only():
    from ctx import ask

    for intent in ("locate", "impact"):
        plan_json, _ = ask.compile_ask(intent, "q", symbol="X")
        assert json.loads(plan_json)["emit"]["sections"] == ["coverage"]


def test_missing_intent_suggests_but_does_not_run():
    from ctx import ask

    with pytest.raises(ask.AskError) as e:
        ask.compile_ask(None, "why is test_auth failing")
    msg = str(e.value)
    assert "--intent diagnose" in msg and "advisory" in msg


def test_unknown_intent_teaches_close_match():
    from ctx import ask

    with pytest.raises(ask.AskError) as e:
        ask.compile_ask("diagnoze", "q")
    assert "diagnose" in str(e.value)


def test_single_identifier_inferred_disclosed():
    from ctx import ask

    plan_json, disc = ask.compile_ask("locate", "Where is TokenBucket defined")
    assert '"symbol": "TokenBucket"' in plan_json
    assert any("inferred" in d for d in disc)


def test_ambiguous_subject_refuses_with_candidates():
    from ctx import ask

    with pytest.raises(ask.AskError) as e:
        ask.compile_ask("locate", "How do AuthContext and TokenBucket relate")
    m = str(e.value)
    assert "ambiguous" in m and "AuthContext" in m and "TokenBucket" in m


def test_no_identifier_refuses_asks_for_symbol():
    from ctx import ask

    with pytest.raises(ask.AskError) as e:
        ask.compile_ask("locate", "where is the thing")
    assert "--symbol" in str(e.value)


def test_infer_symbol_skips_capitalized_english():
    from ctx import ask

    # "Where" is capitalized English, not CamelCase; only AuthContext qualifies.
    inferred, cands = ask.infer_symbol("Where is AuthContext")
    assert inferred == "AuthContext" and cands == ["AuthContext"]
    # A single plain capitalized word is never a subject.
    inferred2, cands2 = ask.infer_symbol("Where is Something")
    assert inferred2 is None and cands2 == []


# --------------------------------------------------------- the thin ops
def test_code_symbols_returns_structured_rows(state_home, workspace_dir):
    from ctx.plan_ops import OPS, PlanContext

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "m.py").write_text(
        "class Cache:\n    def build(self, k):\n        return k\n", encoding="utf-8"
    )
    pc = PlanContext(ws=ws, store=store)
    out = OPS["code.symbols"].fn(pc, {"file": "m.py"}, None)
    assert out["kind"] == "symbols"
    names = {r["symbol"] for r in out["rows"]}
    assert any("Cache" in n for n in names)
    # Rows are structured (range + span), not rendered outline text.
    row = out["rows"][0]
    assert "line" in row and "line_b" in row and "kind" in row


def test_code_context_is_terminal_text(state_home, workspace_dir):
    from ctx.plan_ops import OPS, PlanContext

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "m.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    pc = PlanContext(ws=ws, store=store)
    inp = {"kind": "sites", "rows": [{"file": "m.py", "line": 2}]}
    out = OPS["code.context"].fn(pc, {"context": 1}, inp)
    assert out["kind"] == "text"  # the closure law: bytes enter terminally
    assert out["meta"]["refinement"] == "terminal"
    assert "b = 2" in out["rows"][0]["text"]


def test_evidence_failures_empty_teaches(state_home, workspace_dir):
    from ctx.plan_ops import OPS, PlanContext

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    pc = PlanContext(ws=ws, store=store)
    out = OPS["evidence.failures"].fn(pc, {}, None)
    assert out["kind"] == "sites" and out["rows"] == []
    assert "no captured failures" in out["meta"]["note"]


# --------------------------------------------- end to end: diagnose, no rerun
@pytest.fixture()
def failing_capture(tmp_path, state_home):
    """A committed baseline + a raise inside the changed function, with the
    failing pytest run captured under the birth gate (so the deepest frame
    lands in changed source — the root-cause join's shape)."""
    ws = tmp_path / "proj"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / "cache.py").write_text(
        "def build(k):\n    return k.strip().lower()\n", encoding="utf-8"
    )
    (ws / "test_cache.py").write_text(
        "from cache import build\n\n\ndef test_build():\n"
        "    assert build(' A ') == 'a'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    (ws / "cache.py").write_text(
        "def build(k):\n    raise ValueError('cache key rejected')\n", encoding="utf-8"
    )
    from ctx.execution import run_capture
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    wsobj = resolve_workspace(str(ws))
    store = Store(wsobj.workspace_id)
    run_capture(wsobj, [VENV_PY, "-m", "pytest", "-q"], store=store, timeout=120)
    return wsobj, store


def test_diagnose_end_to_end_no_rerun(failing_capture):
    from ctx import ask
    from ctx.plan_exec import execute_plan

    ws, store = failing_capture
    plan_json, disclosure = ask.compile_ask("diagnose", "Why is test_build failing")
    out, code = execute_plan(ws, store, plan_json, tier="cli")
    assert code == 0
    # The culprit is named — root-cause join hit the changed function.
    assert "conclusion candidates (census): 1" in out
    assert "build" in out and "cache.py" in out and "ValueError" in out
    # The failure came from captured facts, not a fresh run: the coverage
    # names evidence.failures against a run:, and no test.run node exists.
    assert "evidence.failures" in out
    assert "test.run" not in out
    assert any("diagnose" in d for d in disclosure)


def test_diagnose_declares_staleness(failing_capture):
    """The observe invariant: evidence.failures never reruns; when the
    worktree generation has moved past the captured facts, it DECLARES the
    staleness and proposes (never runs) a refresh."""
    from ctx.plan_ops import OPS, PlanContext

    ws, store = failing_capture
    pc = PlanContext(ws=ws, store=store)
    # First diagnose derives the fail facts, stamped at the CAPTURE
    # generation (facts derive lazily on first read — the real flow is
    # capture → immediate diagnose → keep editing → diagnose again).
    first = OPS["evidence.failures"].fn(pc, {}, None)
    assert first["rows"] and first["meta"]["fresh"] is True
    # Now the worktree moves: a NEW untracked file (tracked-modified files
    # don't move the generation — that's the generation_hash contract).
    (ws.root / "unrelated_new.py").write_text("z = 9\n", encoding="utf-8")
    out = OPS["evidence.failures"].fn(pc, {}, None)
    assert out["rows"]  # still reads the captured failure, never reruns
    assert out["meta"]["fresh"] is False
    assert "rerun tests to refresh" in out["meta"]["note"]


def test_cli_ask_show_plan(failing_capture, capsys):
    from ctx.cli import main

    ws, _ = failing_capture
    rc = main(["--workspace", str(ws.root), "ask",
               "Why is test_build failing", "--intent", "diagnose", "--plan"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ctx ask]" in out and "intent: diagnose" in out
    assert '"ctx.plan/v1"' in out and "evidence.failures" in out
