"""Acceptance: the reflex arc v1 (docs/REFLEX.md layers 1-3).

Contracts under test:
* signature normalization — slicer decorations (`| head`, `| tail`,
  `| grep`, `2>&1`) and flag noise (`--tb=`, `-x`, `-v`) never change a
  command's signature; the spec3 re-run loop collapses to ONE signature,
  while a different test file stays a different signature;
* detector semantics — first run records an intervention (no event);
  re-issue fires a starvation event, latches densify, returns "densify";
  the latch persists for the session; a `ctx get` on a known run handle
  records a landing;
* the FROZEN outcome-ledger schema (the scorecard reader builds on it);
* determinism — reflex state and ledger (minus ts) are a pure function of
  the session's command sequence;
* fail-open — unwritable ledger dir changes nothing, and the hook still
  emits exactly one valid decision JSON with reflexes active;
* densify plumbing — a re-run through the real CLI prints the densified
  header and dense=True reaches the DigestContext (the pytest profile's
  dense rendering itself is owned elsewhere and not asserted here).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

LEDGER = ".ctx-session-reads"
OUTCOMES = "reflex-outcomes.jsonl"


def _events(root: Path) -> list[dict]:
    path = Path(root) / LEDGER / OUTCOMES
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _invoke_hook(payload: str, flavor: str = "antigravity") -> dict:
    """Run the real hook entry end-to-end (stdin JSON → stdout JSON)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", flavor, "pre-tool-use"],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    assert len(lines) == 1, f"hook must emit exactly one JSON object, got: {proc.stdout!r}"
    return json.loads(lines[0])


def _bash_payload(command: str, workspace: Path) -> str:
    return json.dumps(
        {
            "tool_name": "run_command",
            "tool_input": {"CommandLine": command, "Cwd": str(workspace)},
            "workspacePaths": [str(workspace)],
        }
    )


# ----------------------------------------------------- (a) signatures


def test_signature_collapses_spec3_slicer_loop():
    from ctx.reflex import command_signature

    base = command_signature("python -m pytest tests/x.py -v")
    assert base == "pytest tests/x.py"
    same = [
        "python -m pytest tests/x.py -v | head -100",
        "python -m pytest tests/x.py --tb=short 2>&1 | tail -50",
        "python -m pytest tests/x.py -x -q 2>&1",
        "pytest tests/x.py --tb=line | grep FAILED",
        "pytest tests/x.py 2>&1 | grep FAIL | head -5",
    ]
    for cmd in same:
        assert command_signature(cmd) == base, cmd
    # A different test file is a different behavioral target.
    assert command_signature("python -m pytest tests/y.py") != base
    assert command_signature("python -m pytest tests/y.py") == "pytest tests/y.py"


def test_signature_unwraps_wrappers_shells_and_ctx_run():
    from ctx.reflex import command_signature

    base = command_signature("pytest tests/x.py")
    assert command_signature("timeout 60 pytest tests/x.py -v") == base
    assert command_signature("env FOO=1 pytest tests/x.py") == base
    assert command_signature("bash -c 'pytest tests/x.py | grep FAIL'") == base
    # Guard-rewritten re-runs keep the raw command's signature.
    assert command_signature("ctx run -- pytest tests/x.py -v") == base
    assert (
        command_signature("ctx run --shell -- 'pytest tests/x.py --tb=short 2>&1 | head -50'")
        == base
    )


def test_signature_none_for_empty_and_ctx_retrieval():
    from ctx.reflex import command_signature

    assert command_signature("") is None
    assert command_signature("   ") is None
    assert command_signature("ctx get run:abc123 --lines 1:5") is None
    assert command_signature("ctx search run:abc123 FAIL") is None
    assert command_signature("ctx stats repo:") is None


def test_landing_ref_detection():
    from ctx.reflex import landing_ref

    assert landing_ref("ctx get run:abc123def456 --lines 1:40") == "run:abc123def456"
    assert landing_ref("ctx search run:abc123 FAILED") == "run:abc123"
    assert landing_ref("ctx get 'run:abc123#stdout'") == "run:abc123"
    assert landing_ref("ctx get repo:src/x.py --lines 1:5") is None
    assert landing_ref("ctx stats run:abc123") is None  # get/search only
    assert landing_ref("pytest tests/x.py") is None


# ------------------------------------- (b)(c) starvation detector + latch


def test_first_run_intervention_only_then_starvation_and_latch(tmp_path):
    from ctx import reflex

    sig = reflex.command_signature("pytest tests/x.py")
    reflex.note_intervention(tmp_path, sig, "abc123def456", hints=2)
    assert _events(tmp_path) == []  # an intervention alone is not an outcome

    # A different signature is not starvation.
    assert reflex.check_command(tmp_path, "pytest tests/y.py") is None
    assert _events(tmp_path) == []
    assert not reflex.densify_latched(tmp_path, sig)

    # Same signature re-issued with a slicer: the spec3 loop, caught at
    # occurrence 2.
    assert reflex.check_command(tmp_path, "pytest tests/x.py | head -100") == "densify"
    evs = _events(tmp_path)
    assert len(evs) == 1
    assert evs[0]["event"] == "starvation"
    assert evs[0]["signature"] == sig
    assert evs[0]["run"] == "abc123def456"
    assert evs[0]["action"] == "densify"
    assert reflex.densify_latched(tmp_path, sig)


def test_latch_persists_and_events_dedupe_per_intervention_cycle(tmp_path):
    from ctx import reflex

    sig = reflex.command_signature("pytest tests/x.py")
    reflex.note_intervention(tmp_path, sig, "aaaa11112222")
    assert reflex.check_command(tmp_path, "pytest tests/x.py --tb=line") == "densify"
    # Hammering the same signature within one intervention cycle stays one
    # event (the hook and `ctx run` both sight the same physical re-run);
    # the latch and the densify return persist.
    assert reflex.check_command(tmp_path, "pytest tests/x.py -x") == "densify"
    assert reflex.check_command(tmp_path, "pytest tests/x.py") == "densify"
    assert len(_events(tmp_path)) == 1
    assert reflex.densify_latched(tmp_path, sig)

    # The next digest render (a fresh intervention) re-arms the detector:
    # a further re-run is a fresh starvation event — densify isn't working,
    # and the slow loop needs to know.
    reflex.note_intervention(tmp_path, sig, "bbbb33334444")
    assert reflex.check_command(tmp_path, "pytest tests/x.py") == "densify"
    evs = _events(tmp_path)
    assert [e["event"] for e in evs] == ["starvation", "starvation"]
    assert evs[1]["run"] == "bbbb33334444"
    assert reflex.densify_latched(tmp_path, sig)  # latching: never unlatches

    state = reflex.read_state(tmp_path)
    assert state["densify"] == {sig: True}
    assert state["interventions"][sig]["count"] == 2
    assert state["outcomes_appended"] == 2


# ----------------------------------------------------------- (d) landings


def test_landing_event_on_known_handle(tmp_path):
    from ctx import reflex

    sig = "pytest tests/x.py"
    reflex.note_intervention(tmp_path, sig, "abc123def456")
    reflex.note_landing(tmp_path, "run:abc123def456#stdout")
    evs = _events(tmp_path)
    assert len(evs) == 1
    assert evs[0] == {
        "ts": evs[0]["ts"],
        "event": "landing",
        "signature": sig,
        "run": "abc123def456",
        "action": "none",
    }
    # Prefix matching: a shorter handle for the same run still lands.
    reflex.note_landing(tmp_path, "run:abc123")
    assert len(_events(tmp_path)) == 2
    # An unknown handle is not a landing on any recorded intervention.
    reflex.note_landing(tmp_path, "run:ffffffffffff")
    assert len(_events(tmp_path)) == 2
    # Landings clear nothing: no latch, interventions intact.
    assert not reflex.densify_latched(tmp_path, sig)
    assert reflex.read_state(tmp_path)["interventions"][sig]["count"] == 1


# ------------------------------------------------- (f) frozen ledger schema


def test_ledger_lines_match_frozen_schema_exactly(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "abc123def456")
    reflex.check_command(tmp_path, "pytest tests/x.py | head -20")
    reflex.note_landing(tmp_path, "run:abc123def456")

    raw = (tmp_path / LEDGER / OUTCOMES).read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        obj = json.loads(ln)
        assert set(obj) == {"ts", "event", "signature", "run", "action"}
        assert list(obj) == sorted(obj)  # sort_keys=True on the wire
        assert isinstance(obj["ts"], float)
        assert obj["event"] in ("starvation", "landing", "friction")
        assert isinstance(obj["signature"], str) and obj["signature"]
        assert obj["run"] is None or isinstance(obj["run"], str)
        assert obj["action"] in ("densify", "none")
    assert json.loads(lines[0])["event"] == "starvation"
    assert json.loads(lines[0])["action"] == "densify"
    assert json.loads(lines[1])["event"] == "landing"
    assert json.loads(lines[1])["action"] == "none"


# ---------------------------------------------------------- determinism


def test_state_and_ledger_pure_function_of_command_sequence(tmp_path):
    from ctx import reflex

    def drive(root: Path) -> None:
        root.mkdir(exist_ok=True)
        reflex.note_intervention(root, "pytest tests/x.py", "abc123def456", hints=1)
        reflex.check_command(root, "pytest tests/x.py | head -100")
        reflex.note_landing(root, "run:abc123def456")
        reflex.note_intervention(root, "pytest tests/x.py", "abc123def456", hints=1)
        reflex.check_command(root, "pytest tests/x.py --tb=line")

    a, b = tmp_path / "a", tmp_path / "b"
    drive(a)
    drive(b)
    state_a = (a / LEDGER / "reflex.json").read_text(encoding="utf-8")
    state_b = (b / LEDGER / "reflex.json").read_text(encoding="utf-8")
    assert state_a == state_b  # byte-identical state
    strip = lambda evs: [{k: v for k, v in e.items() if k != "ts"} for e in evs]
    assert strip(_events(a)) == strip(_events(b))  # ledger identical minus ts


# ------------------------------------------------------------ (g) fail-open


def test_all_io_fail_open_when_ledger_dir_unwritable(tmp_path):
    from ctx import reflex

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / LEDGER).write_text("a file, not a directory", encoding="utf-8")

    reflex.note_intervention(ws, "pytest tests/x.py", "abc123def456")  # no crash
    assert reflex.check_command(ws, "pytest tests/x.py") is None
    reflex.note_landing(ws, "run:abc123def456")  # no crash
    assert reflex.densify_latched(ws, "pytest tests/x.py") is False
    assert (ws / LEDGER).read_text(encoding="utf-8") == "a file, not a directory"

    # The hook still emits exactly one valid decision on this workspace.
    (ws / "ctx.toml").write_text('version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8")
    d = _invoke_hook(_bash_payload("pytest tests/x.py", ws))
    assert d["decision"] == "deny"
    assert "ctx run -- pytest tests/x.py" in d["reason"]


# ------------------------------------------------- (h) hook end-to-end


def test_hook_starvation_path_decision_unchanged_ledger_written(tmp_path):
    from ctx import reflex

    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )
    sig = reflex.command_signature("pytest tests/x.py")
    reflex.note_intervention(tmp_path, sig, "abc123def456")

    # Re-issue with a slicer through the REAL hook: decision is exactly the
    # classifier's (reflexes never block), and the starvation event lands.
    d = _invoke_hook(_bash_payload("pytest tests/x.py -x | head -100", tmp_path))
    assert d["decision"] == "force_ask"  # compound shell, steering=deny
    evs = _events(tmp_path)
    assert [e["event"] for e in evs] == ["starvation"]
    assert reflex.densify_latched(tmp_path, sig)

    # Plain re-run in the same cycle: deny decision intact, event deduped.
    d2 = _invoke_hook(_bash_payload("pytest tests/x.py", tmp_path))
    assert d2["decision"] == "deny"
    assert [e["event"] for e in _events(tmp_path)] == ["starvation"]


def test_hook_landing_path_allows_and_records(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "abc123def456")
    d = _invoke_hook(_bash_payload("ctx get run:abc123def456 --lines 1:30", tmp_path))
    assert d["decision"] == "allow"  # ctx-routed commands always allowed
    evs = _events(tmp_path)
    assert [e["event"] for e in evs] == ["landing"]
    assert evs[0]["signature"] == "pytest tests/x.py"


def test_hook_claude_code_flavor_valid_with_reflex_active(tmp_path):
    from ctx import reflex

    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )
    reflex.note_intervention(tmp_path, "pytest tests/x.py", "abc123def456")
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/x.py | tail -50"},
            "cwd": str(tmp_path),
            "workspacePaths": [str(tmp_path)],
        }
    )
    out = _invoke_hook(payload, flavor="claude-code")
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] in ("allow", "deny", "ask")
    assert [e["event"] for e in _events(tmp_path)] == ["starvation"]


# --------------------------------------------- (e) densify plumbing via CLI


def test_cli_rerun_prints_densified_header_and_dense_reaches_context(
    state_home, workspace_dir, monkeypatch, capsys
):
    """Two identical failing runs through the real CLI: the first records an
    intervention; the second detects starvation, renders dense, and declares
    it. Asserts the PLUMBING (dense=True on the DigestContext) — the pytest
    profile's dense rendering is owned by pytestprof and not asserted."""
    import ctx.digest as digest_mod

    seen: list[bool] = []
    orig = digest_mod.detect_profile

    def spy(dctx):
        seen.append(bool(getattr(dctx, "dense", False)))
        return orig(dctx)

    monkeypatch.setattr(digest_mod, "detect_profile", spy)

    from ctx.cli import main

    script = "for i in range(3000): print('line', i)\nraise SystemExit(1)"
    argv = ["--workspace", str(workspace_dir), "run", "--", sys.executable, "-c", script]

    rc1 = main(argv)
    out1 = capsys.readouterr().out
    assert rc1 == 3  # failing run
    assert "omitted" in out1  # the digest omitted content → intervention
    assert not out1.startswith("densified:")
    assert _events(workspace_dir) == []  # first run: no starvation

    rc2 = main(argv)
    out2 = capsys.readouterr().out
    assert rc2 == 3
    assert out2.startswith("densified: re-run detected · full evidence inline\n")
    # The declared header is printed ABOVE the stored digest — content
    # identity (the "[ctx run:...]" header and body) is untouched by reflex.
    assert out2.splitlines()[1].startswith("[ctx run:")
    assert seen == [False, True]  # dense flag reached the DigestContext

    evs = _events(workspace_dir)
    assert [e["event"] for e in evs] == ["starvation"]
    assert evs[0]["action"] == "densify"

    from ctx import reflex

    state = reflex.read_state(workspace_dir)
    assert len(state["interventions"]) == 1
    sig = next(iter(state["interventions"]))
    assert state["interventions"][sig]["count"] == 2  # both runs intervened
    assert state["densify"] == {sig: True}


def test_cli_dense_rendering_identity_is_deterministic(
    state_home, workspace_dir, capsys
):
    """Same command sequence → identical printed digests (minus the run id
    line, which is content-hash stable anyway): densification selects WHICH
    deterministic rendering, never a nondeterministic one."""
    from ctx.cli import main

    script = "for i in range(3000): print('line', i)\nraise SystemExit(1)"
    argv = ["--workspace", str(workspace_dir), "run", "--", sys.executable, "-c", script]
    main(argv)
    capsys.readouterr()
    main(argv)
    out_a = capsys.readouterr().out
    main(argv)
    out_b = capsys.readouterr().out
    assert out_a == out_b  # dense re-runs of identical bytes: byte-identical
