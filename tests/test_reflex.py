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
  dense rendering itself is owned elsewhere and not asserted here);
* Controller State wave (shadow): the v2 intervention ledger dual-writes
  through the real CLI with zero cli changes (defaulted plan fields) —
  full shadow-detector coverage lives in tests/test_generations.py and
  tests/test_intervention_events.py;
* ctx q visibility (the ALGEBRA live-A/B gap) — `ctx q '<pipeline>'`
  signatures normalize (quote/whitespace/--trace variance collapses; stage
  names+args are kept, they ARE the semantics); the q-dry ledger the query
  engine writes folds fail-open into additive ctx.q/v1 events
  (dry_query_rerun on an identical re-issue after a 0-row result;
  recovered on rows-after-dry — the landing extension); absent ledger →
  nothing scored, nothing written;
* the ab_algebra_live referee (frozen constants checksum, mechanical
  grading, spec3-reused aggregation, taught-vs-untaught doctrine gates) —
  unit-tested on synthetic rows, never a live session.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
EVALS = Path(__file__).resolve().parent.parent / "evals"

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


# ------------------------------------------------------------- reflex v2
def test_edit_disarms_starvation(tmp_path):
    """run → digest → EDIT → re-run is verification, not starvation (the
    spec3 round-2 false-positive class)."""
    from ctx import reflex

    ws = tmp_path
    (ws / ".ctx-session-reads").mkdir()
    reflex.note_intervention(ws, "pytest tests/x.py", "abc123def456")
    reflex.note_edit(ws)  # the model edited code
    assert reflex.check_command(ws, "python -m pytest tests/x.py -v") is None
    ledger = ws / ".ctx-session-reads" / "reflex-outcomes.jsonl"
    assert not ledger.exists()  # no starvation event, no densify action


def test_rerun_without_edit_still_scores(tmp_path):
    from ctx import reflex

    ws = tmp_path
    (ws / ".ctx-session-reads").mkdir()
    reflex.note_intervention(ws, "pytest tests/x.py", "abc123def456")
    assert reflex.check_command(ws, "python -m pytest tests/x.py | head -50") == "densify"


def test_next_digest_rearms_after_edit(tmp_path):
    """edit disarms; the following run's digest re-arms; a slicer re-run
    after THAT digest scores starvation again."""
    from ctx import reflex

    ws = tmp_path
    (ws / ".ctx-session-reads").mkdir()
    reflex.note_intervention(ws, "pytest tests/x.py", "aaa111aaa111")
    reflex.note_edit(ws)
    assert reflex.check_command(ws, "pytest tests/x.py") is None  # verification
    reflex.note_intervention(ws, "pytest tests/x.py", "bbb222bbb222")  # its digest
    assert reflex.check_command(ws, "pytest tests/x.py --tb=line") == "densify"


def test_densify_latch_survives_disarm(tmp_path):
    """A genuinely-starved signature keeps its dense rendering through
    later edit cycles (latching by design)."""
    from ctx import reflex

    ws = tmp_path
    (ws / ".ctx-session-reads").mkdir()
    sig = "pytest tests/x.py"
    reflex.note_intervention(ws, sig, "aaa111aaa111")
    assert reflex.check_command(ws, "pytest tests/x.py | tail -5") == "densify"
    reflex.note_edit(ws)
    assert reflex.check_command(ws, "pytest tests/x.py") is None  # no new event
    assert reflex.densify_latched(ws, sig) is True  # rendering stays dense


def test_cli_run_dual_writes_v2_intervention_ledger(
    state_home, workspace_dir, capsys
):
    """Controller State wave: the v2 intervention ledger (EDC §9) is
    dual-written through the REAL cli path with zero cli changes —
    ``note_intervention`` emits the ctx.intervention/v1 line with plan
    fields defaulted from the densify latch (normal → dense across the
    starvation), and the shadow rerun outcome lands as a provisional
    (non-git workspace ⇒ unknown generation) equivalent_rerun. The v1
    ledger and printed digests stay untouched (covered above)."""
    from ctx.cli import main

    script = "for i in range(3000): print('line', i)\nraise SystemExit(1)"
    argv = ["--workspace", str(workspace_dir), "run", "--", sys.executable, "-c", script]
    main(argv)
    main(argv)
    capsys.readouterr()

    path = workspace_dir / LEDGER / "interventions.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    emissions = [e for e in events if e.get("event") == "intervention_emitted"]
    outcomes = [e for e in events if e.get("event") == "intervention_outcome"]
    assert len(emissions) == 2  # one per omission-bearing digest
    assert [e["planMode"] for e in emissions] == ["normal", "dense"]
    assert all(e["planId"] is None for e in emissions)  # richer plan data:
    # cli passes the resolver's plan_id/mode + coverage receipt in a later
    # integration wave; the defaulted dual-write keeps the pipeline live.
    assert all(e["schema"] == "ctx.intervention/v1" for e in emissions)
    assert emissions[0]["artifact"] and len(emissions[0]["artifact"]) == 12
    assert [o["outcome"] for o in outcomes] == ["equivalent_rerun"]
    assert outcomes[0]["evidence"]["generation"] == "unknown"  # non-git ws
    assert outcomes[0]["evidence"]["confirmed"] is False  # provisional


def test_hook_edit_tool_disarms_and_allows(tmp_path):
    """End-to-end: an Edit tool event through the claude-code hook disarms
    the signature and emits a valid allow decision."""
    import io
    import json as _json
    import sys as _sys

    from ctx import reflex
    from ctx.hook import main_pre_tool_use

    ws = tmp_path
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / ".ctx-session-reads").mkdir()
    reflex.note_intervention(ws, "pytest tests/x.py", "abc123def456")

    payload = _json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(ws / "mod.py")},
        "cwd": str(ws),
    })
    old_in, old_out = _sys.stdin, _sys.stdout
    _sys.stdin, _sys.stdout = io.StringIO(payload), io.StringIO()
    try:
        assert main_pre_tool_use(flavor="claude-code") == 0
        out = _sys.stdout.getvalue()
    finally:
        _sys.stdin, _sys.stdout = old_in, old_out
    decision = _json.loads(out)
    assert decision  # exactly one valid JSON decision
    assert reflex.check_command(ws, "pytest tests/x.py") is None  # disarmed


# ---------------------------------------------- ctx q signatures (algebra)


def test_q_signature_normalizes_quote_whitespace_trace_variants():
    """The live-A/B loop (3 identical dry `ctx q` reruns) collapses to ONE
    signature regardless of quoting, whitespace, --trace, or trailing
    slicer pipes; a different pipeline stays a different signature."""
    from ctx.reflex import command_signature

    base = command_signature("ctx q 'fails last | in-changed'")
    assert base == "q fails last | in-changed"
    same = [
        'ctx q "fails last | in-changed"',
        "ctx q   'fails   last |  in-changed '",
        "ctx q --trace 'fails last | in-changed'",
        "ctx q 'fails last | in-changed' --trace",
        "ctx q 'fails last | in-changed' | head -5",
        "bash -c \"ctx q 'fails last | in-changed'\"",
    ]
    for cmd in same:
        assert command_signature(cmd) == base, cmd
    # Stage names AND args are the semantics — different pipelines differ.
    assert command_signature("ctx q 'fails last | shared-cause'") != base
    assert (
        command_signature("ctx q 'refs TokenBucket | group file | top 3'")
        == "q refs TokenBucket | group file | top 3"
    )
    # Inner-arg quoting variance is one signature too.
    assert command_signature(
        "ctx q 'decls --kind function | where file=src/alpha.py'"
    ) == command_signature(
        'ctx q "decls --kind function | where file=src/alpha.py"'
    )
    # Empty pipelines have no signature; other retrieval verbs unchanged.
    assert command_signature("ctx q") is None
    assert command_signature("ctx q --trace") is None
    assert command_signature("ctx q ''") is None
    assert command_signature("ctx get run:abc123 --lines 1:5") is None
    assert command_signature("ctx search run:abc123 FAIL") is None
    assert command_signature("ctx run -- pytest tests/x.py -v") == "pytest tests/x.py"


def test_query_signature_matches_command_signature():
    """The ledger-writer helper and the hook-side signature agree
    byte-for-byte (ledger keys must match sighted commands)."""
    from ctx.reflex import command_signature, query_signature

    assert (
        query_signature("fails  last |   in-changed")
        == command_signature("ctx q 'fails last | in-changed'")
    )
    assert query_signature("") is None
    assert query_signature(None) is None


# ------------------------------------------- q-dry ledger → ctx.q/v1 events

Q_SIG = "q fails last | in-changed"


def _q_events(root: Path) -> list[dict]:
    path = Path(root) / LEDGER / "interventions.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(ln)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def _write_q_ops(root: Path, lines: list[dict], append: bool = False) -> None:
    led = Path(root) / LEDGER
    led.mkdir(exist_ok=True)
    mode = "a" if append else "w"
    with (led / "q-dry.jsonl").open(mode, encoding="utf-8") as fh:
        for rec in lines:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")


def test_dry_query_rerun_events_from_op_lines(tmp_path):
    """The ledger-driven path: the query engine's q_dry_rerun op lines
    fold into ctx.q/v1 dry_query_rerun events — once each (cursor), no
    matter how often the fold runs. The v1 outcome ledger (FROZEN schema)
    is never touched by q events."""
    from ctx import reflex

    _write_q_ops(tmp_path, [
        {"op": "q", "signature": Q_SIG, "rows": 0, "ts": 1.0},
        {"op": "q_dry_rerun", "signature": Q_SIG, "rows": 0, "ts": 2.0},
        {"op": "q_dry_rerun", "signature": Q_SIG, "rows": 0, "ts": 3.0},
    ])
    assert reflex.check_command(tmp_path, "ctx q 'fails last | in-changed'") is None
    evs = _q_events(tmp_path)
    assert [e["event"] for e in evs] == ["dry_query_rerun", "dry_query_rerun"]
    assert all(e["schema"] == "ctx.q/v1" for e in evs)
    assert all(e["signature"] == Q_SIG for e in evs)
    assert all(e["rows"] == 0 for e in evs)
    # Cursor: a second sighting re-folds nothing.
    reflex.check_command(tmp_path, "ctx q 'fails last | in-changed'")
    assert len(_q_events(tmp_path)) == 2
    # Retrieval purity: q reruns are NOT §8 starvation — v1 ledger silent.
    assert _events(tmp_path) == []


def test_recovered_event_after_dry(tmp_path):
    """The landing extension: rows>0 following a dry identical pipeline is
    a 'recovered' positive (the teaching worked)."""
    from ctx import reflex

    _write_q_ops(tmp_path, [
        {"op": "q", "signature": Q_SIG, "rows": 0, "ts": 1.0},
        {"op": "q", "signature": Q_SIG, "rows": 4, "ts": 2.0},
    ])
    reflex.sync_query_outcomes(tmp_path)
    evs = _q_events(tmp_path)
    assert [(e["event"], e["rows"]) for e in evs] == [("recovered", 4)]
    # A dry result alone (no rerun, no recovery) is not an event.
    _write_q_ops(tmp_path, [
        {"op": "q", "signature": "q decls", "rows": 0, "ts": 3.0},
    ], append=True)
    reflex.sync_query_outcomes(tmp_path)
    assert len(_q_events(tmp_path)) == 1


def test_q_engine_ledger_contract_end_to_end(tmp_path):
    """The ACTUAL writer contract (ctx.query's self-healing wave): op
    lines carry the RAW pipeline text under "pipeline" (no rows key);
    q-dry.json is {"dry": [<raw pipeline text>...]}, and a pipeline
    leaving that census after a non-empty result is the recovery signal
    (rows unknown → null)."""
    from ctx import reflex

    raw = "fails  last |   in-changed"  # raw text normalizes to Q_SIG
    led = tmp_path / LEDGER
    led.mkdir()
    (led / "q-dry.json").write_text(json.dumps({"dry": [raw]}), encoding="utf-8")
    (led / "q-dry.jsonl").write_text(
        json.dumps({"op": "q_dry_rerun", "pipeline": raw, "ts": 1.0}) + "\n",
        encoding="utf-8",
    )
    reflex.check_command(tmp_path, "ctx q 'fails last | in-changed'")
    evs = _q_events(tmp_path)
    assert [(e["event"], e["signature"], e["rows"]) for e in evs] == [
        ("dry_query_rerun", Q_SIG, 0)
    ]
    # The engine drops the pipeline from the dry census on its first
    # non-empty result: recovered (row count unknown → null).
    (led / "q-dry.json").write_text(json.dumps({"dry": []}), encoding="utf-8")
    reflex.sync_query_outcomes(tmp_path)
    evs = _q_events(tmp_path)
    assert [(e["event"], e["rows"]) for e in evs] == [
        ("dry_query_rerun", 0), ("recovered", None),
    ]
    # Census now empty and nothing dry: further syncs are silent.
    reflex.sync_query_outcomes(tmp_path)
    assert len(_q_events(tmp_path)) == 2


def test_q_real_engine_handshake(state_home, workspace_dir):
    """Cross-module integration: the REAL query engine executes an
    identical dry pipeline twice (writing its q-dry ledger); reflex folds
    it into exactly one dry_query_rerun event — the live-A/B loop, now
    visible end-to-end."""
    import pytest as _pytest

    query = _pytest.importorskip("ctx.query")
    from conftest import make_store, make_ws

    from ctx import reflex

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
    pipeline = "search zzz_matches_nothing"
    out1, code1 = query.run_query(ws, store, pipeline)
    assert code1 == 0 and "rows (census): 0" in out1
    out2, code2 = query.run_query(ws, store, pipeline)  # identical dry rerun
    assert code2 == 0
    sig = reflex.query_signature(pipeline)
    assert reflex.check_command(workspace_dir, f"ctx q '{pipeline}'") is None
    evs = _q_events(workspace_dir)
    assert [(e["event"], e["signature"]) for e in evs] == [
        ("dry_query_rerun", sig)
    ]
    # The pipeline recovers (matches appear): the engine drops it from the
    # dry census; the next fold ledgers the recovered positive.
    (workspace_dir / "b.py").write_text("zzz_matches_nothing = 2\n", encoding="utf-8")
    out3, code3 = query.run_query(ws, store, pipeline)
    assert code3 == 0 and "rows (census): 1" in out3
    reflex.sync_query_outcomes(workspace_dir)
    evs = _q_events(workspace_dir)
    assert [e["event"] for e in evs] == ["dry_query_rerun", "recovered"]


def test_q_provisional_mapping_shape_tolerated(tmp_path):
    """The provisional {"pipelines": {sig: {"rows": N}}} state shape stays
    readable: rows 0 marks dryness (no event — a dry result alone is not
    a rerun); rows>0 after dry is recovered with the count."""
    from ctx import reflex

    led = tmp_path / LEDGER
    led.mkdir()
    (led / "q-dry.json").write_text(
        json.dumps({"pipelines": {Q_SIG: {"rows": 0}}}), encoding="utf-8"
    )
    reflex.check_command(tmp_path, "ctx q 'fails last | in-changed'")
    assert _q_events(tmp_path) == []  # dryness recorded, no event yet
    (led / "q-dry.json").write_text(
        json.dumps({"pipelines": {Q_SIG: {"rows": 5}}}), encoding="utf-8"
    )
    reflex.check_command(tmp_path, "ctx q 'decls'")
    evs = _q_events(tmp_path)
    assert [(e["event"], e["rows"]) for e in evs] == [("recovered", 5)]


def test_q_ledger_absent_scores_nothing_and_writes_nothing(tmp_path):
    """No q-dry ledger → skip entirely (never guess): no events, and the
    ledger dir is NOT created (worktreeHash golden discipline)."""
    from ctx import reflex

    ws = tmp_path / "ws"
    ws.mkdir()
    assert reflex.check_command(ws, "ctx q 'fails last | in-changed'") is None
    reflex.sync_query_outcomes(ws)
    assert not (ws / LEDGER).exists()


def test_q_ledger_fail_open_on_garbage(tmp_path):
    """Corrupt ledger shapes change nothing and never raise."""
    from ctx import reflex

    led = tmp_path / LEDGER
    led.mkdir()
    (led / "q-dry.json").write_text("{not json", encoding="utf-8")
    (led / "q-dry.jsonl").mkdir()  # a directory, not a file
    assert reflex.check_command(tmp_path, "ctx q 'fails last | in-changed'") is None
    assert _q_events(tmp_path) == []
    # Corrupt op LINES are skipped individually; the cursor still advances.
    import shutil

    shutil.rmtree(led / "q-dry.jsonl")
    (led / "q-dry.json").unlink()
    (led / "q-dry.jsonl").write_text(
        "not json at all\n"
        + json.dumps({"op": "q_dry_rerun", "signature": Q_SIG, "rows": 0}) + "\n",
        encoding="utf-8",
    )
    reflex.sync_query_outcomes(tmp_path)
    reflex.sync_query_outcomes(tmp_path)  # idempotent past corrupt lines
    assert [e["event"] for e in _q_events(tmp_path)] == ["dry_query_rerun"]


def test_q_events_do_not_touch_run_signature_machinery(tmp_path):
    """Retrieval purity, both directions: a q sighting neither trips the
    starvation detector for run signatures nor consumes their hypothesis
    windows; run interventions keep scoring exactly as before."""
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "abc123def456")
    _write_q_ops(tmp_path, [
        {"op": "q_dry_rerun", "signature": Q_SIG, "rows": 0, "ts": 1.0},
    ])
    assert reflex.check_command(tmp_path, "ctx q 'fails last | in-changed'") is None
    assert not reflex.densify_latched(tmp_path, "pytest tests/x.py")
    assert _events(tmp_path) == []  # no v1 starvation from the q sighting
    # The run detector still fires exactly as before.
    assert reflex.check_command(tmp_path, "pytest tests/x.py -x") == "densify"
    assert [e["event"] for e in _events(tmp_path)] == ["starvation"]


def test_q_state_and_events_deterministic(tmp_path):
    """Same ledger content + same sighting sequence → byte-identical state
    and identical events minus ts (the determinism contract extends)."""
    from ctx import reflex

    def drive(root: Path) -> None:
        root.mkdir(exist_ok=True)
        _write_q_ops(root, [
            {"op": "q", "signature": Q_SIG, "rows": 0, "ts": 1.0},
            {"op": "q_dry_rerun", "signature": Q_SIG, "rows": 0, "ts": 2.0},
            {"op": "q", "signature": Q_SIG, "rows": 3, "ts": 3.0},
        ])
        reflex.check_command(root, "ctx q 'fails last | in-changed'")

    a, b = tmp_path / "a", tmp_path / "b"
    drive(a)
    drive(b)
    state_a = (a / LEDGER / "reflex.json").read_text(encoding="utf-8")
    state_b = (b / LEDGER / "reflex.json").read_text(encoding="utf-8")
    assert state_a == state_b
    strip = lambda evs: [{k: v for k, v in e.items() if k != "ts"} for e in evs]
    assert strip(_q_events(a)) == strip(_q_events(b))
    assert strip(_q_events(a)) == [
        {"schema": "ctx.q/v1", "event": "dry_query_rerun",
         "signature": Q_SIG, "rows": 0},
        {"schema": "ctx.q/v1", "event": "recovered",
         "signature": Q_SIG, "rows": 3},
    ]


# ------------------------------- ab_algebra_live referee (no live sessions)


def _ab():
    sys.path.insert(0, str(EVALS))
    import ab_algebra_live

    return ab_algebra_live


AB_ALGEBRA_FROZEN_SHA256 = (
    "143252d3afd4f42bbb9b51f78c49ec6fdd958c716631e2639790493f98c8b012"
)


def test_ab_algebra_frozen_referee_constants():
    """The frozen-referee contract, algebra edition (mirrors
    test_scorecard_v2.py::test_frozen_referee_constants): TASK_PROMPT,
    TEACH, fixture files, ground truth, and arm construction are the
    n>=3 taught-vs-untaught referee — any drift invalidates cross-round
    comparison against the live-A/B receipt, so it fails loudly here and
    the fix is to revert the drift, not to update the hash."""
    import hashlib

    ab = _ab()
    h = hashlib.sha256()
    h.update(ab.TASK_PROMPT.encode())
    h.update(ab.TEACH.encode())
    h.update(json.dumps(ab.BASE_FILES, sort_keys=True).encode())
    h.update(json.dumps(ab.INTRODUCED_EDITS, sort_keys=True).encode())
    h.update(json.dumps(
        [list(ab.INTRODUCED_TESTS), list(ab.PREEXISTING_TESTS)]).encode())
    for arm in ("taught", "untaught"):
        h.update(json.dumps(ab.arm_argv(arm, "haiku")).encode())
    assert h.hexdigest() == AB_ALGEBRA_FROZEN_SHA256, (
        "ab_algebra frozen-referee constants drifted — TASK_PROMPT/TEACH/"
        "fixture/ground-truth/arm_argv must not change (cross-round "
        "comparisons die)."
    )
    # The receipt's structural requirements, asserted not just hashed:
    # both arms are ctx wrap; the ONLY delta is the appended teach.
    taught = ab.arm_argv("taught", "haiku")
    untaught = ab.arm_argv("untaught", "haiku")
    assert taught[:3] == ["ctx", "wrap", "claude"]
    assert untaught == [t for t in taught if t not in ("--append-system-prompt", ab.TEACH)]
    assert "--append-system-prompt" in taught and ab.TEACH in taught
    assert str(ab.MAX_TURNS) == "25" or ab.MAX_TURNS == 25


def test_ab_algebra_grade_format():
    ab = _ab()
    good = "some prose\nINTRODUCED: test_add, test_scale\nPREEXISTING: test_median\n"
    g = ab.grade_format(good)
    assert g["format_present"] and g["format_correct"]
    # Node-id spellings and reordering are tolerated.
    g2 = ab.grade_format(
        "INTRODUCED: tests/test_suite.py::test_scale, tests/test_suite.py::test_add\n"
        "PREEXISTING: `test_median`."
    )
    assert g2["format_present"] and g2["format_correct"]
    # Wrong classification: present but not correct.
    g3 = ab.grade_format(
        "INTRODUCED: test_add, test_median\nPREEXISTING: test_scale\n"
    )
    assert g3["format_present"] and not g3["format_correct"]
    # Missing a line: not present, not correct.
    g4 = ab.grade_format("INTRODUCED: test_add, test_scale\n")
    assert not g4["format_present"] and not g4["format_correct"]
    # Empty / dead session output.
    g5 = ab.grade_format("")
    assert not g5["format_present"] and not g5["format_correct"]
    # The LAST occurrence wins (models often restate at the end).
    g6 = ab.grade_format(
        "INTRODUCED: test_median\nPREEXISTING: test_add\n...fixing...\n"
        "INTRODUCED: test_add, test_scale\nPREEXISTING: test_median\n"
    )
    assert g6["format_correct"]


def test_ab_algebra_fix_grading():
    ab = _ab()
    out = (
        "FAILED tests/test_suite.py::test_median - assert 3 == 2.5\n"
        "1 failed, 4 passed in 0.12s\n"
    )
    assert ab.failing_tests(out) == {"test_median"}
    assert ab.fixes_correct({"test_median"}) is True
    assert ab.fixes_correct({"test_median", "test_add"}) is False
    assert ab.fixes_correct(set()) is False  # "fixed" the pre-existing bug too
    # Collection errors count as failing.
    assert ab.failing_tests("ERROR tests/test_suite.py::test_scale - boom\n") == {
        "test_scale"
    }


def _ab_row(arm, rep, turns, fmt, fixes=True, cost=0.1, wall=60.0, cache=97.0):
    return {"task": "algebra_classify", "arm": arm, "rep": rep, "turns": turns,
            "cost_usd": cost, "wall_s": wall, "cache_hit_pct": cache,
            "format_present": bool(fmt), "format_correct": bool(fmt),
            "fixes_correct": bool(fixes), "correct": bool(fmt and fixes),
            "session_error": False}


def test_ab_algebra_gates_pass_on_synthetic_rows():
    """Aggregation is REUSED from the frozen spec3 runner; the doctrine
    gates read its medians block."""
    ab = _ab()
    rows = [
        _ab_row("taught", 1, 10, True), _ab_row("taught", 2, 12, True),
        _ab_row("taught", 3, 14, True),
        _ab_row("untaught", 1, 14, True), _ab_row("untaught", 2, 15, True),
        _ab_row("untaught", 3, 16, True),
    ]
    medians = ab.aggregate_rows(rows)  # the spec3 import, unchanged
    assert medians["algebra_classify/taught"]["turns"]["median"] == 12
    assert medians["algebra_classify/untaught"]["turns"]["median"] == 15
    gates, ok = ab.evaluate_ab_gates(rows, medians)
    assert ok and all(g["ok"] for g in gates)
    assert [g["gate"] for g in gates] == [
        "taught_turns<=untaught_turns", "taught_format>=untaught_format",
    ]


def test_ab_algebra_gates_fail_on_regression():
    ab = _ab()
    # The n=1 receipt shape: taught costs turns AND drops the format.
    rows = [
        _ab_row("taught", 1, 26, False), _ab_row("taught", 2, 24, True),
        _ab_row("taught", 3, 25, False),
        _ab_row("untaught", 1, 20, True), _ab_row("untaught", 2, 18, True),
        _ab_row("untaught", 3, 21, True),
    ]
    gates, ok = ab.evaluate_ab_gates(rows, ab.aggregate_rows(rows))
    assert not ok
    assert [g["ok"] for g in gates] == [False, False]
    # Equality passes both gates (<=, >=): parity is not a loss.
    rows_eq = [
        _ab_row("taught", 1, 20, True),
        _ab_row("untaught", 1, 20, True),
    ]
    gates_eq, ok_eq = ab.evaluate_ab_gates(rows_eq, ab.aggregate_rows(rows_eq))
    assert ok_eq


def test_ab_algebra_gates_fail_closed_on_missing_inputs():
    ab = _ab()
    rows = [_ab_row("taught", 1, 10, True)]  # no untaught arm at all
    gates, ok = ab.evaluate_ab_gates(rows, ab.aggregate_rows(rows))
    assert not ok and all(not g["ok"] for g in gates)
    # Failed sessions (turns None) drop out of medians → turns gate FAILS
    # closed rather than comparing invented numbers.
    rows2 = [
        _ab_row("taught", 1, None, False),
        _ab_row("untaught", 1, 20, True),
    ]
    gates2, ok2 = ab.evaluate_ab_gates(rows2, ab.aggregate_rows(rows2))
    assert not ok2
    assert gates2[0]["ok"] is False  # turns: fail closed
    assert gates2[1]["ok"] is False  # format: 0/1 < 1/1


def test_ab_algebra_fixture_ground_truth(tmp_path):
    """The frozen fixture's ground truth, verified with REAL pytest (no
    live session): base commit fails exactly the pre-existing gamma test;
    the working tree adds exactly the two introduced src-frame failures."""
    ab = _ab()
    base = tmp_path / "base"
    ab.make_fixture(base, introduced=False)
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-rf", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=base, capture_output=True, text=True, timeout=180,
    )
    assert ab.failing_tests(p.stdout + p.stderr) == set(ab.PREEXISTING_TESTS)

    work = tmp_path / "work"
    ab.make_fixture(work)
    p2 = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-rf", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=work, capture_output=True, text=True, timeout=180,
    )
    assert ab.failing_tests(p2.stdout + p2.stderr) == set(
        ab.INTRODUCED_TESTS
    ) | set(ab.PREEXISTING_TESTS)
    # The receipt's frame requirement: introduced failures raise IN src/.
    assert "src/alpha.py" in p2.stdout and "src/beta.py" in p2.stdout
    # The introduced edits are uncommitted (the classification axis).
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=work,
        capture_output=True, text=True,
    ).stdout
    assert " M src/alpha.py" in porcelain and " M src/beta.py" in porcelain
