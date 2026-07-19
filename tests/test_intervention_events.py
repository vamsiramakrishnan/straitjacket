"""Intervention events (docs/EDC.md §9), per-family signature tables (§7),
the shadow circuit state machine (§10), and graduated-steering shadow
(phase 6b) — Controller State wave, everything SHADOW.

Contracts under test:

* the FROZEN v2 ledger schemas (``.ctx-session-reads/interventions.jsonl``)
  — emission (ctx.intervention/v1) and outcome
  (ctx.intervention-outcome/v1) lines with exactly the keys the scorecard
  v2 reader parses, and the scorecard actually parsing them;
* deterministic interventionIds: sha256("<sessionSeq>|<signature>")[:12] —
  same command sequence, same ids (replay holds);
* dual-write: the v1 reflex-outcomes ledger and reflex state stay
  byte-compatible; ``note_intervention`` emits the v2 line with plan
  fields defaulted from the densify latch (cli integration passes richer
  plan data in a later wave);
* hypothesis windows: expiry after 3 tool-bearing commands or a generation
  change → ``expired_unresolved`` (censored — never a resolution);
* pytest scope flags KEPT in signatures (-k/-m/--lf/--ff/--deselect),
  presentation flags dropped; ``is_narrower`` containment; a narrower
  rerun scores ``narrowed_execution`` (Rule 9b), never starvation;
* circuit: one transition per (signature × generation) episode, hysteresis
  (bypass→dense after 2 positives, dense→normal after 3), transitions
  recorded as shadow events, live rendering still latch-driven;
* steering shadow: ``steer_shadow`` lines with ``would_bypass`` =
  engagement passive AND no prior flood; NO decision/rewrite change;
* fail-open everywhere; the hook emits valid decision JSON on all paths.
"""

import hashlib
import io
import json
import sys
from pathlib import Path

LEDGER = ".ctx-session-reads"

EMISSION_KEYS = {
    "schema", "event", "interventionId", "sessionSeq", "family", "signature",
    "generation", "artifact", "planId", "planMode", "coverage", "hints", "ts",
}
OUTCOME_KEYS = {"schema", "event", "interventionId", "outcome", "evidence", "ts"}


def _lines(root: Path, name: str) -> list[dict]:
    path = Path(root) / LEDGER / name
    if not path.is_file():
        return []
    return [
        json.loads(ln)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def _v2(root: Path) -> list[dict]:
    return _lines(root, "interventions.jsonl")


def _emissions(root: Path) -> list[dict]:
    return [e for e in _v2(root) if e.get("event") == "intervention_emitted"]


def _outcomes(root: Path) -> list[dict]:
    return [e for e in _v2(root) if e.get("event") == "intervention_outcome"]


def _transitions(root: Path) -> list[dict]:
    return [e for e in _v2(root) if e.get("event") == "circuit_transition"]


# ------------------------------------------------------ frozen v2 schemas


def test_emission_line_matches_frozen_schema_exactly(tmp_path):
    from ctx import reflex

    iid = reflex.emit_intervention(
        tmp_path,
        family="pytest",
        signature="pytest tests/x.py",
        generation="g-1",
        artifact_run_id="abc123def456",
        plan_id="deadbeef1234",
        plan_mode="dense",
        coverage={"requiredFraction": 1.0, "named": [8, 8]},
        hints=2,
    )
    assert isinstance(iid, str) and len(iid) == 12
    raw = (tmp_path / LEDGER / "interventions.jsonl").read_text(encoding="utf-8")
    (line,) = [ln for ln in raw.splitlines() if ln.strip()]
    obj = json.loads(line)
    assert set(obj) == EMISSION_KEYS
    assert list(obj) == sorted(obj)  # sort_keys on the wire
    assert obj["schema"] == "ctx.intervention/v1"
    assert obj["event"] == "intervention_emitted"
    assert obj["interventionId"] == iid
    assert obj["sessionSeq"] == 1
    assert obj["family"] == "pytest"
    assert obj["signature"] == "pytest tests/x.py"
    assert obj["generation"] == "g-1"
    assert obj["artifact"] == "abc123def456"
    assert obj["planId"] == "deadbeef1234"
    assert obj["planMode"] == "dense"
    assert obj["coverage"] == {"requiredFraction": 1.0, "named": [8, 8]}
    assert obj["hints"] == 2
    assert isinstance(obj["ts"], float)


def test_outcome_line_matches_frozen_schema_exactly(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    reflex.check_command(tmp_path, "pytest tests/x.py", generation="g")
    outs = _outcomes(tmp_path)
    assert len(outs) == 1
    obj = outs[0]
    assert set(obj) == OUTCOME_KEYS
    assert obj["schema"] == "ctx.intervention-outcome/v1"
    assert obj["event"] == "intervention_outcome"
    assert obj["outcome"] == "equivalent_rerun"
    assert isinstance(obj["evidence"], dict)
    assert isinstance(obj["ts"], float)


def test_intervention_id_deterministic_derivation(tmp_path):
    from ctx import reflex

    sig = "pytest tests/x.py"
    iid = reflex.emit_intervention(tmp_path, signature=sig, generation="g")
    assert iid == hashlib.sha256(f"1|{sig}".encode()).hexdigest()[:12]
    iid2 = reflex.emit_intervention(tmp_path, signature=sig, generation="g")
    assert iid2 == hashlib.sha256(f"2|{sig}".encode()).hexdigest()[:12]


def test_same_command_sequence_same_ids_and_ledger(tmp_path):
    """Replay determinism: two directories driven identically produce
    identical v2 ledgers minus ts (and identical state bytes)."""
    from ctx import reflex

    def drive(root: Path) -> None:
        root.mkdir()
        reflex.note_intervention(root, "pytest tests/x.py", "a" * 12, generation="g1")
        reflex.check_command(root, "pytest tests/x.py | head -5", generation="g1")
        reflex.note_intervention(root, "pytest tests/x.py", "b" * 12, generation="g1")
        reflex.check_command(root, "pytest tests/x.py::TestA::test_b", generation="g2")
        reflex.note_landing(root, "run:" + "b" * 12)

    a, b = tmp_path / "a", tmp_path / "b"
    drive(a)
    drive(b)
    strip = lambda evs: [{k: v for k, v in e.items() if k != "ts"} for e in evs]
    assert strip(_v2(a)) == strip(_v2(b))
    state_a = (a / LEDGER / "reflex.json").read_text(encoding="utf-8")
    state_b = (b / LEDGER / "reflex.json").read_text(encoding="utf-8")
    assert state_a == state_b


# ------------------------------------------------- dual-write + defaults


def test_note_intervention_dual_writes_with_defaulted_plan_fields(tmp_path):
    from ctx import reflex

    reflex.note_intervention(
        tmp_path, "pytest tests/x.py", "a" * 12, hints=3, generation="g1"
    )
    (em,) = _emissions(tmp_path)
    assert em["planId"] is None  # cli passes richer plan data in a later wave
    assert em["planMode"] == "normal"  # not latched yet
    assert em["coverage"] == {}
    assert em["hints"] == 3
    assert em["family"] == "pytest"
    assert em["artifact"] == "a" * 12
    assert em["generation"] == "g1"

    # Starvation latches densify → the NEXT emission defaults planMode dense.
    reflex.check_command(tmp_path, "pytest tests/x.py", generation="g1")
    reflex.note_intervention(
        tmp_path, "pytest tests/x.py", "b" * 12, generation="g1"
    )
    ems = _emissions(tmp_path)
    assert [e["planMode"] for e in ems] == ["normal", "dense"]


def test_v1_ledger_and_state_stay_byte_compatible(tmp_path):
    """Dual-write: the FROZEN v1 reflex-outcomes schema is untouched by the
    v2 machinery running alongside."""
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    reflex.check_command(tmp_path, "pytest tests/x.py | head -20", generation="g")
    reflex.note_landing(tmp_path, "run:" + "a" * 12)
    v1 = _lines(tmp_path, "reflex-outcomes.jsonl")
    assert len(v1) == 2
    for obj in v1:
        assert set(obj) == {"ts", "event", "signature", "run", "action"}
    assert v1[0]["event"] == "starvation" and v1[0]["action"] == "densify"
    assert v1[1]["event"] == "landing" and v1[1]["action"] == "none"
    state = reflex.read_state(tmp_path)
    rec = state["interventions"]["pytest tests/x.py"]
    assert rec["count"] == 1 and rec["starved"] is True and rec["armed"] is True
    assert state["densify"] == {"pytest tests/x.py": True}


def test_emit_intervention_never_arms_the_live_detector(tmp_path):
    """A direct (shadow-plane) emission for a signature the v1 pipeline
    never intervened on must not create live starvation."""
    from ctx import reflex

    reflex.emit_intervention(tmp_path, signature="pytest tests/x.py", generation="g")
    assert reflex.check_command(tmp_path, "pytest tests/x.py", generation="g") is None
    assert _lines(tmp_path, "reflex-outcomes.jsonl") == []  # no v1 starvation
    assert reflex.densify_latched(tmp_path, "pytest tests/x.py") is False


# --------------------------------------------- hypothesis windows (§9)


def test_window_expires_after_three_tool_bearing_commands(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    reflex.check_command(tmp_path, "ls src", generation="g")
    reflex.check_command(tmp_path, "mkdir build", generation="g")
    assert _outcomes(tmp_path) == []  # 2 commands: still open
    reflex.check_command(tmp_path, "go build ./...", generation="g")
    (o,) = _outcomes(tmp_path)
    assert o["outcome"] == "expired_unresolved"
    assert o["evidence"] == {"reason": "window_elapsed"}


def test_window_expires_on_generation_change_at_next_emission(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g1")
    reflex.note_intervention(tmp_path, "go test ./...", "b" * 12, generation="g2")
    outs = _outcomes(tmp_path)
    assert len(outs) == 1
    assert outs[0]["outcome"] == "expired_unresolved"
    assert outs[0]["evidence"] == {"reason": "generation_change"}
    ems = _emissions(tmp_path)
    assert outs[0]["interventionId"] == ems[0]["interventionId"]


def test_fresh_emission_supersedes_open_window_for_same_signature(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    reflex.note_intervention(tmp_path, "pytest tests/x.py", "b" * 12, generation="g")
    (o,) = _outcomes(tmp_path)
    assert o["outcome"] == "expired_unresolved"
    assert o["evidence"] == {"reason": "superseded"}


def test_expired_is_censored_never_a_resolution():
    from ctx.scorecard import _resolution

    assert _resolution(["expired_unresolved"]) is None
    assert _resolution(["expired_unresolved", "retrieval_landing"]) == "retrieval_landing"


def test_resolution_closes_window_once_per_cycle(tmp_path):
    """Hammering the same signature within one intervention cycle scores ONE
    shadow outcome (mirrors the v1 per-cycle dedup)."""
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    reflex.check_command(tmp_path, "pytest tests/x.py -x", generation="g")
    reflex.check_command(tmp_path, "pytest tests/x.py -q", generation="g")
    reflex.check_command(tmp_path, "pytest tests/x.py", generation="g")
    assert len(_outcomes(tmp_path)) == 1


def test_landing_resolves_window_with_retrieval_landing(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "abc123def456", generation="g")
    reflex.note_landing(tmp_path, "run:abc123def456#stdout")
    (o,) = _outcomes(tmp_path)
    assert o["outcome"] == "retrieval_landing"
    assert o["evidence"]["handle"] == "run:abc123def456"
    # v1 landing line untouched next to it
    v1 = _lines(tmp_path, "reflex-outcomes.jsonl")
    assert [e["event"] for e in v1] == ["landing"]


# ------------------------------------- §7: scope flags and is_narrower


def test_pytest_scope_flags_kept_presentation_flags_dropped():
    from ctx.reflex import command_signature

    base = command_signature("pytest tests/x.py")
    assert base == "pytest tests/x.py"
    # Presentation flags still normalize away (v1 behavior kept).
    assert command_signature("pytest tests/x.py -v --tb=short -x -q") == base
    # Scope flags are kept WITH their values — a scope change is a new
    # signature, never starvation (the 748f470aa1 defect).
    k = command_signature("pytest tests/x.py -k auth")
    assert k == "pytest tests/x.py -k auth"
    assert k != base
    assert command_signature("pytest tests/x.py -m slow") == "pytest tests/x.py -m slow"
    assert command_signature("pytest tests/x.py --lf") == "pytest tests/x.py --lf"
    assert command_signature("pytest tests/x.py --ff") == "pytest tests/x.py --ff"
    assert (
        command_signature("pytest tests/x.py --deselect tests/x.py::t")
        == "pytest tests/x.py --deselect tests/x.py::t"
    )
    # Spelling and position normalization: one signature per scope.
    assert command_signature("pytest tests/x.py -k=auth") == k
    assert command_signature("pytest -k auth tests/x.py -v") == k
    assert command_signature("python -m pytest tests/x.py -k auth 2>&1 | head -5") == k
    # Distinct scope values stay distinct.
    assert command_signature("pytest tests/x.py -k auth") != command_signature(
        "pytest tests/x.py -k users"
    )


def test_scope_flag_rerun_is_not_starvation(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    # Same file, new -k scope: a DIFFERENT signature (and materially
    # narrower → Rule 9b positive, asserted elsewhere) — the live detector
    # must not fire.
    assert (
        reflex.check_command(tmp_path, "pytest tests/x.py -k auth", generation="g")
        is None
    )
    assert _lines(tmp_path, "reflex-outcomes.jsonl") == []


def test_is_narrower_pytest_containment():
    from ctx.reflex import is_narrower

    assert is_narrower("pytest tests/x.py::TestA::test_b", "pytest tests/x.py")
    assert is_narrower("pytest tests/x.py::TestA", "pytest tests/x.py")
    assert is_narrower("pytest tests/x.py", "pytest")
    assert is_narrower("pytest tests/unit/test_a.py", "pytest tests")
    assert is_narrower("pytest tests/x.py -k auth", "pytest tests/x.py")
    assert is_narrower("pytest tests/x.py --lf", "pytest tests/x.py")
    # NOT narrower: equality, broadening, disjoint scopes, other families.
    assert not is_narrower("pytest tests/x.py", "pytest tests/x.py")
    assert not is_narrower("pytest", "pytest tests/x.py")
    assert not is_narrower("pytest tests/y.py", "pytest tests/x.py")
    assert not is_narrower("pytest tests/x.py", "pytest tests/x.py -k auth")
    assert not is_narrower("pytest tests/xy.py", "pytest tests/x.py")
    assert not is_narrower("go test ./pkg", "go test")
    assert not is_narrower(None, "pytest")
    assert not is_narrower("pytest", None)


def test_narrowed_execution_shadow_outcome_never_starvation(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    live = reflex.check_command(
        tmp_path, "pytest tests/x.py::TestA::test_b -x", generation="g"
    )
    assert live is None  # never a live starvation / densify
    (o,) = _outcomes(tmp_path)
    assert o["outcome"] == "narrowed_execution"
    assert o["evidence"]["narrower_signature"] == "pytest tests/x.py::TestA::test_b"
    assert o["evidence"]["signature"] == "pytest tests/x.py"
    assert o["evidence"]["generation"] == "equal"
    assert _lines(tmp_path, "reflex-outcomes.jsonl") == []  # no v1 starvation
    assert reflex.densify_latched(tmp_path, "pytest tests/x.py") is False


# --------------------------------------------- §10: circuit (shadow)


def test_circuit_one_transition_per_episode_then_escalates_next_generation(tmp_path):
    from ctx import reflex

    sig = "pytest tests/x.py"
    # Episode 1 (g1): repeated confirmed starvation → exactly ONE transition.
    reflex.note_intervention(tmp_path, sig, "a" * 12, generation="g1")
    reflex.check_command(tmp_path, "pytest tests/x.py | head -5", generation="g1")
    reflex.note_intervention(tmp_path, sig, "b" * 12, generation="g1")
    reflex.check_command(tmp_path, "pytest tests/x.py --tb=line", generation="g1")
    reflex.note_intervention(tmp_path, sig, "c" * 12, generation="g1")
    reflex.check_command(tmp_path, "pytest tests/x.py -x", generation="g1")
    trans = _transitions(tmp_path)
    assert [(t["from"], t["to"]) for t in trans] == [("normal", "dense")]
    assert reflex.circuit_state(tmp_path, sig) == "dense"

    # Episode 2 (g2): post-densify starvation in a NEW generation → the
    # breaker concession, dense→bypass (again exactly once).
    reflex.note_intervention(tmp_path, sig, "d" * 12, generation="g2")
    reflex.check_command(tmp_path, "pytest tests/x.py", generation="g2")
    reflex.note_intervention(tmp_path, sig, "e" * 12, generation="g2")
    reflex.check_command(tmp_path, "pytest tests/x.py -q", generation="g2")
    trans = _transitions(tmp_path)
    assert [(t["from"], t["to"]) for t in trans] == [
        ("normal", "dense"), ("dense", "bypass"),
    ]
    assert reflex.circuit_state(tmp_path, sig) == "bypass"
    for t in trans:
        assert t["schema"] == "ctx.circuit/v1"
        assert t["shadow"] is True
        assert t["family"] == "pytest"
        assert t["signature"] == sig


def test_circuit_hysteresis_earns_recovery(tmp_path):
    """bypass→dense after 2 positive outcomes, dense→normal after 3."""
    from ctx import reflex

    sig = "pytest tests/x.py"

    def starve(gen: str, run: str) -> None:
        reflex.note_intervention(tmp_path, sig, run * 12, generation=gen)
        reflex.check_command(tmp_path, "pytest tests/x.py", generation=gen)

    def positive(gen_emit: str, gen_rerun: str, run: str) -> None:
        # A rerun in a LATER generation is validation_after_edit — a
        # hysteresis positive.
        reflex.note_intervention(tmp_path, sig, run * 12, generation=gen_emit)
        reflex.check_command(tmp_path, "pytest tests/x.py", generation=gen_rerun)

    starve("g1", "a")  # normal → dense
    starve("g2", "b")  # dense → bypass
    assert reflex.circuit_state(tmp_path, sig) == "bypass"

    positive("g3", "g4", "c")  # positive 1 — no move yet
    assert reflex.circuit_state(tmp_path, sig) == "bypass"
    positive("g4", "g5", "d")  # positive 2 — bypass → dense
    assert reflex.circuit_state(tmp_path, sig) == "dense"

    positive("g5", "g6", "e")  # 1
    positive("g6", "g7", "f")  # 2
    assert reflex.circuit_state(tmp_path, sig) == "dense"
    positive("g7", "g8", "0")  # 3 — dense → normal
    assert reflex.circuit_state(tmp_path, sig) == "normal"

    assert [(t["from"], t["to"]) for t in _transitions(tmp_path)] == [
        ("normal", "dense"), ("dense", "bypass"),
        ("bypass", "dense"), ("dense", "normal"),
    ]


def test_circuit_is_shadow_only_rendering_stays_latch_driven(tmp_path):
    """The resolver reads reflex state's ``densify``/``bypass`` keys; the
    shadow machine lives under ``circuit_shadow`` and must NEVER leak into
    live plan selection this wave."""
    from ctx import reflex
    from ctx.resolver import session_state

    sig = "pytest tests/x.py"
    for gen, run in (("g1", "a"), ("g2", "b")):
        reflex.note_intervention(tmp_path, sig, run * 12, generation=gen)
        reflex.check_command(tmp_path, "pytest tests/x.py", generation=gen)
    assert reflex.circuit_state(tmp_path, sig) == "bypass"  # shadow says bypass
    state = reflex.read_state(tmp_path)
    assert "bypass" not in state  # the LIVE circuit key was never written
    assert state["densify"] == {sig: True}  # rendering driver: the v2 latch
    assert session_state(tmp_path, sig).circuit == "dense"  # resolver: latch only


# ------------------------------------------- scorecard v2 parser match


def test_scorecard_v2_reader_parses_this_ledger(tmp_path):
    """The schemas as implemented feed the committed scorecard reader:
    families, outcomes, censoring, transitions, and episodes all fold."""
    from ctx import reflex
    from ctx.scorecard import _interventions_v2

    sig = "pytest tests/x.py"
    reflex.note_intervention(tmp_path, sig, "abc123def456", hints=2, generation="g1")
    reflex.check_command(tmp_path, "pytest tests/x.py | head -5", generation="g1")
    reflex.note_intervention(tmp_path, sig, "bbb222bbb222", generation="g1")
    reflex.note_landing(tmp_path, "run:bbb222bbb222")
    reflex.note_intervention(tmp_path, sig, "ccc333ccc333", generation="g1")
    reflex.check_command(tmp_path, "ls a", generation="g1")
    reflex.check_command(tmp_path, "ls b", generation="g1")
    reflex.check_command(tmp_path, "ls c", generation="g1")  # → expired

    iv = _interventions_v2(tmp_path / LEDGER)
    assert iv is not None
    fam = iv["families"]["pytest"]
    assert fam["events"] == 3
    assert fam["slicer_reruns"] == 1
    assert fam["landings"] == 1
    assert fam["expired"] == 1
    assert fam["hinted"] == 1  # only the first emission carried hints
    assert fam["hinted_resolved"] == 1
    assert fam["transitions"] == {"dense": 1}  # normal→dense folded by target
    (ep,) = iv["episodes"]
    assert ep["signature"] == sig
    assert ep["family"] == "pytest"
    assert ep["generation"] == "g1"
    assert ep["responses"] == ["normal", "dense"]
    # Outcomes in emission order: rerun, landing, then the censored expiry.
    assert ep["outcomes"] == ["slicer_rerun", "retrieval_landing", "expired_unresolved"]


# ------------------------------------------- phase 6b: steering shadow


def test_steer_shadow_would_bypass_logic(tmp_path):
    from ctx import reflex

    # Fresh session: engagement passive, no prior flood → would bypass.
    reflex.note_steer_shadow(tmp_path, "pytest tests/x.py -q")
    # Prior flood for the signature → would NOT bypass.
    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    reflex.note_steer_shadow(tmp_path, "pytest tests/x.py -q")
    # Engagement graduated to active → would NOT bypass (other signature).
    from ctx.engagement import note_truncation

    note_truncation(tmp_path)
    reflex.note_steer_shadow(tmp_path, "go test ./...")

    rows = _lines(tmp_path, "steering-shadow.jsonl")
    assert [set(r) for r in rows] == [{"op", "signature", "would_bypass", "ts"}] * 3
    assert all(r["op"] == "steer_shadow" for r in rows)
    assert [r["would_bypass"] for r in rows] == [True, False, False]
    assert rows[0]["signature"] == "pytest tests/x.py"
    assert rows[2]["signature"] == "go test ./..."


def test_hook_records_steer_shadow_on_rewrite_path(tmp_path):
    """End-to-end: an unbounded command under rewrite steering emits a valid
    substitution decision AND one steer_shadow line — behavior unchanged."""
    from ctx.hook import main_pre_tool_use

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/x.py -q"},
        "cwd": str(tmp_path),
        "workspacePaths": [str(tmp_path)],
    })
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(payload), io.StringIO()
    try:
        assert main_pre_tool_use(flavor="claude-code") == 0
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    (line,) = [ln for ln in out.splitlines() if ln.strip()]
    hso = json.loads(line)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"  # steered substitution
    assert "ctx run" in hso["updatedInput"]["command"]
    rows = _lines(tmp_path, "steering-shadow.jsonl")
    assert len(rows) == 1
    assert rows[0]["would_bypass"] is True  # passive + no prior flood


def test_hook_no_steer_shadow_under_deny_steering(tmp_path):
    from ctx.hook import main_pre_tool_use

    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/x.py -q"},
        "cwd": str(tmp_path),
        "workspacePaths": [str(tmp_path)],
    })
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(payload), io.StringIO()
    try:
        assert main_pre_tool_use(flavor="claude-code") == 0
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"  # no rewrite, byte-identical
    assert not (tmp_path / LEDGER / "steering-shadow.jsonl").exists()


# ------------------------------------------------------------ fail-open


def test_all_new_apis_fail_open_when_ledger_unwritable(tmp_path):
    from ctx import reflex

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / LEDGER).write_text("a file, not a directory", encoding="utf-8")

    assert reflex.emit_intervention(ws, signature="pytest tests/x.py") is None
    reflex.note_intervention(ws, "pytest tests/x.py", "a" * 12, generation="g")
    assert reflex.check_command(ws, "pytest tests/x.py", generation="g") is None
    reflex.note_steer_shadow(ws, "pytest tests/x.py")
    assert reflex.circuit_state(ws, "pytest tests/x.py") == "normal"
    assert reflex.is_narrower("pytest a.py::t", "pytest a.py")  # pure fn intact
    assert (ws / LEDGER).read_text(encoding="utf-8") == "a file, not a directory"


def test_check_command_narrowing_through_real_hook_valid_json(tmp_path):
    """Hook end-to-end on the narrowing path: decision valid, narrowing
    shadow outcome recorded, no starvation."""
    from ctx import reflex
    from ctx.hook import main_pre_tool_use

    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )
    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/x.py::TestA::test_b -x"},
        "cwd": str(tmp_path),
        "workspacePaths": [str(tmp_path)],
    })
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(payload), io.StringIO()
    try:
        assert main_pre_tool_use(flavor="claude-code") == 0
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["permissionDecision"] in ("allow", "deny", "ask")
    outs = _outcomes(tmp_path)
    assert [o["outcome"] for o in outs] == ["narrowed_execution"]
    assert _lines(tmp_path, "reflex-outcomes.jsonl") == []
