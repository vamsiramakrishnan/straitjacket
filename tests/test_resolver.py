"""Acceptance: the Delivery Policy Resolver (docs/EDC.md §5.4, §13) and the
LADDERS edge-8 consolidation — seven hand-rolled budget computations in
src/ctx/cli.py replaced by one resolver.

THE SEVEN REPLACED BUDGET SITES (coordinates are pre-change src/ctx/cli.py):

  1. cli.py:502-509  _emit_run_digest — digest/result base choice +
     failure ×factor (served: run foreground, run --bg finalize, and the
     three job finalize/kill/wait emissions)      → _delivery_plan("run")
  2. cli.py:332-334  seq branch — result_tokens + failure ×factor
                                                  → _delivery_plan("seq")
  3. cli.py:722-729  _cmd_eval — digest/result base choice + failure
     ×factor                                      → _delivery_plan("eval")
  4. cli.py:749      _cmd_diff — turn-retrieval budget via
     charge_turn_budget                           → _emit_retrieval
  5. cli.py:762-763  _cmd_map — repo_map --budget + turn-retrieval budget
                                                  → resolve_retrieval_budget
                                                    (requested=) + _emit_retrieval
  6. cli.py:786      _cmd_code (def/refs/diag) — turn-retrieval budget
                                                  → _emit_retrieval
  7. cli.py:851      _cmd_retrieval (search/get/stats) — turn-retrieval
     budget                                       → _emit_retrieval

Byte-identity method: with no reflex state, no window.json, and default
config, the CLI-emitted run/eval/seq digests are compared byte-for-byte
against the LEGACY pipeline computed independently in this file (render +
hand-rolled budget formula + engagement filter + bounded), which is exactly
the pre-change code path.
"""

import json
import sys

import pytest
from conftest import make_store, make_ws


@pytest.fixture()
def ws_store(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def _budgets(**kw):
    from ctx.config import Budgets

    return Budgets(**kw)


def _resolve(outcome="failure", *, contract=None, session=None, environment=None,
             budgets=None, family="run"):
    from ctx import resolver

    return resolver.resolve_delivery(
        outcome,
        family,
        contract_rendering=contract or {},
        session=session or resolver.SessionState(),
        environment=environment or resolver.EnvironmentSignals(),
        config_budgets=budgets or _budgets(),
    )


# ------------------------------------------------------------ determinism
def test_same_inputs_same_plan_and_plan_id():
    a = _resolve("failure", contract={"base_tokens": 480})
    b = _resolve("failure", contract={"base_tokens": 480})
    assert a == b
    assert a.plan_id == b.plan_id
    assert len(a.plan_id) == 12
    int(a.plan_id, 16)  # 12 hex chars


def test_plan_id_ignores_reasons_but_tracks_fields():
    from ctx.resolver import DeliveryPlan

    base = dict(
        mode="fail_census", census="complete", item_summary="one_line",
        inline_detail_count=1, include_addresses=True, include_teaching=True,
        token_budget=960, evidence_floor=480, hard_ceiling=1_000_000,
    )
    p1 = DeliveryPlan(**base, reasons=("outcome_failure",))
    p2 = DeliveryPlan(**base, reasons=("outcome_failure", "failure_multiplier"))
    assert p1.plan_id == p2.plan_id  # reasons excluded from identity
    p3 = DeliveryPlan(**{**base, "token_budget": 961})
    assert p3.plan_id != p1.plan_id  # any non-reason field changes identity


# ------------------------------------------------------------ clamp order
def test_failure_then_pressure_then_floor_then_ceiling():
    """base 480 → ×2 failure = 960 → ×0.5 at 95%% = 480 → floor 480 holds."""
    from ctx import resolver

    plan = _resolve(
        "failure",
        contract={"base_tokens": 480},
        environment=resolver.EnvironmentSignals(window_pct=95.0),
    )
    assert plan.token_budget == 480
    assert plan.reasons == ("outcome_failure", "failure_multiplier", "window_pressure")


def test_pressure_never_squeezes_below_evidence_floor():
    """Floor is applied AFTER the multipliers (EDC §13): at 130%% the ramp
    floors at 0.25 → 960*0.25 = 240, but the default failure floor (the
    digest budget, 480) lifts it back."""
    from ctx import resolver

    plan = _resolve(
        "failure",
        contract={"base_tokens": 480},
        environment=resolver.EnvironmentSignals(window_pct=130.0),
    )
    assert plan.token_budget == 480
    assert "evidence_floor" in plan.reasons
    assert "window_pressure" in plan.reasons


@pytest.mark.parametrize("pct", [70.0, 84.0, 95.0, 110.0, 130.0, 500.0])
def test_floor_holds_at_every_pressure_level(pct):
    from ctx import resolver

    plan = _resolve(
        "failure",
        contract={"base_tokens": 1200, "evidence_floor": 600},
        environment=resolver.EnvironmentSignals(window_pct=pct),
    )
    assert plan.token_budget >= 600


def test_hard_ceiling_applied_last():
    plan = _resolve("failure", contract={"base_tokens": 1200, "hard_ceiling": 900})
    assert plan.token_budget == 900
    assert plan.reasons[-1] == "hard_ceiling"


def test_below_threshold_no_pressure_reason():
    from ctx import resolver

    plan = _resolve(
        "failure",
        contract={"base_tokens": 480},
        environment=resolver.EnvironmentSignals(window_pct=69.9),
    )
    assert plan.token_budget == 960
    assert "window_pressure" not in plan.reasons


def test_floor_gt_ceiling_rejected_at_load_and_fail_open_at_resolve():
    from ctx import resolver

    with pytest.raises(ValueError):
        resolver.validate_rendering_policy({"evidence_floor": 900, "hard_ceiling": 100})
    # The resolver itself never raises: a nonsense contract degrades to the
    # safe default plan mirroring legacy behavior.
    plan = _resolve(
        "failure", contract={"base_tokens": 480, "evidence_floor": 900, "hard_ceiling": 100}
    )
    assert plan.reasons == ("fail_open_default",)
    assert plan.token_budget == 960  # legacy: int(480 * 2.0)


# ------------------------------------------------------------ mode + modes
def test_outcome_maps_to_mode():
    assert _resolve("success").mode == "pass_summary"
    assert _resolve("failure").mode == "fail_census"


def test_circuit_overrides_outcome():
    from ctx import resolver

    dense = _resolve("failure", session=resolver.SessionState(circuit="dense"))
    assert dense.mode == "dense"
    assert "circuit_dense" in dense.reasons
    bypass = _resolve("failure", session=resolver.SessionState(circuit="bypass"))
    assert bypass.mode == "bypass"
    assert "circuit_bypass" in bypass.reasons
    assert bypass.include_teaching is False  # teaching drops before evidence


def test_unfittable_census_escalates_to_flood_never_silent():
    plan = _resolve(
        "failure",
        contract={"base_tokens": 480, "hard_ceiling": 500, "census_min_tokens": 5000},
    )
    assert plan.mode == "flood"
    assert "census_unfittable" in plan.reasons
    assert plan.census == "bounded"  # hierarchically compacted, identity-preserving


def test_addresses_never_suppressed_in_any_mode():
    """EDC §12 correction 1: include_addresses governs teaching prose only;
    no resolver state may emit a plan that suppresses contract-driven
    addresses — pass_summary with retrievable classes included."""
    from ctx import resolver

    for outcome in ("success", "failure"):
        for circuit in ("normal", "dense", "bypass"):
            plan = _resolve(
                outcome,
                contract={"retrievable_nonempty": True},
                session=resolver.SessionState(circuit=circuit),
            )
            assert plan.include_addresses is True, (outcome, circuit)


# ------------------------------------------------------ closed vocabulary
def test_reason_vocabulary_is_closed():
    from ctx.resolver import DeliveryPlan

    with pytest.raises(ValueError):
        DeliveryPlan(
            mode="fail_census", census="complete", item_summary="one_line",
            inline_detail_count=1, include_addresses=True, include_teaching=True,
            token_budget=960, evidence_floor=0, hard_ceiling=1_000_000,
            reasons=("because I felt like it",),
        )


def test_bad_literals_rejected():
    from ctx.resolver import DeliveryPlan

    kw = dict(
        inline_detail_count=0, include_addresses=True, include_teaching=True,
        token_budget=480, evidence_floor=0, hard_ceiling=1_000_000,
    )
    with pytest.raises(ValueError):
        DeliveryPlan(mode="verbose", census="none", item_summary="none", **kw)
    with pytest.raises(ValueError):
        DeliveryPlan(mode="dense", census="partial", item_summary="none", **kw)
    with pytest.raises(ValueError):
        DeliveryPlan(mode="dense", census="none", item_summary="prose", **kw)


def test_all_emitted_reasons_in_vocabulary():
    from ctx import resolver

    grid = [
        _resolve(o, contract=c, environment=e, session=s)
        for o in ("success", "failure")
        for c in ({}, {"base_tokens": 60, "hard_ceiling": 50, "census_min_tokens": 999})
        for e in (resolver.EnvironmentSignals(), resolver.EnvironmentSignals(window_pct=120.0))
        for s in (resolver.SessionState(), resolver.SessionState(circuit="bypass"))
    ]
    for plan in grid:
        assert set(plan.reasons) <= set(resolver.REASON_VOCABULARY)


# ------------------------------------------------- reader capability (§6)
def test_reader_drop_latches_inline(tmp_path):
    from ctx import resolver

    resolver.note_reader_drop(tmp_path)
    sess = resolver.session_state(tmp_path)
    assert sess.reader_latched_inline is True
    # confidence 0 → below the floor → epoch/config default wins anyway;
    # give it confidence to observe the latch itself.
    sess2 = resolver.SessionState(
        reader_latched_inline=True, confidence=0.9, followthrough=0.0, landings=0
    )
    assert resolver.infer_reader_capability(sess2) == "inline"


def test_reader_recovery_is_earned_needs_both_signals():
    from ctx import resolver

    latched = dict(reader_latched_inline=True, confidence=0.9)
    # followthrough alone is not enough
    s = resolver.SessionState(**latched, followthrough=0.9, landings=1)
    assert resolver.infer_reader_capability(s) == "inline"
    # landings alone are not enough
    s = resolver.SessionState(**latched, followthrough=0.5, landings=5)
    assert resolver.infer_reader_capability(s) == "inline"
    # boundary: followthrough must be strictly > 0.7
    s = resolver.SessionState(**latched, followthrough=0.7, landings=2)
    assert resolver.infer_reader_capability(s) == "inline"
    # both → recovery earned
    s = resolver.SessionState(**latched, followthrough=0.71, landings=2)
    assert resolver.infer_reader_capability(s) == resolver.READER_DEFAULT_CAPABILITY


def test_reader_confidence_floor_defers_to_default():
    from ctx import resolver

    s = resolver.SessionState(
        reader_latched_inline=True, reader_preference="inline",
        confidence=0.29, followthrough=0.0, landings=0,
    )
    assert resolver.infer_reader_capability(s) == resolver.READER_DEFAULT_CAPABILITY
    assert resolver.infer_reader_capability(s, default="one_line") == "one_line"


def test_reader_recovery_persisted_via_evidence(tmp_path):
    from ctx import resolver

    resolver.note_reader_drop(tmp_path)
    assert resolver.session_state(tmp_path).reader_latched_inline
    resolver.note_reader_evidence(tmp_path, followthrough=0.9, landings=1)
    assert resolver.session_state(tmp_path).reader_latched_inline  # not yet earned
    resolver.note_reader_evidence(tmp_path, followthrough=0.9, landings=2)
    assert not resolver.session_state(tmp_path).reader_latched_inline


# ------------------------------------------------------ signal fail-open
def test_window_reader_fail_open(tmp_path):
    from ctx import resolver

    assert resolver.read_window(None) == (None, None)
    assert resolver.read_window(tmp_path) == (None, None)  # missing
    d = tmp_path / ".ctx-session-reads" / "proxy"
    d.mkdir(parents=True)
    (d / "window.json").write_text("{broken", encoding="utf-8")
    assert resolver.read_window(tmp_path) == (None, None)  # corrupt
    (d / "window.json").write_text(
        json.dumps({"window_pct": 84.5, "model": "claude-sonnet-5"}), encoding="utf-8"
    )
    env = resolver.environment_signals(tmp_path)
    assert env == resolver.EnvironmentSignals(window_pct=84.5, model_id="claude-sonnet-5")


def test_session_state_reads_reflex_latches_readonly(tmp_path):
    from ctx import resolver

    led = tmp_path / ".ctx-session-reads"
    led.mkdir()
    (led / "reflex.json").write_text(
        json.dumps({"densify": {"pytest tests": True}, "bypass": {"go test": True}}),
        encoding="utf-8",
    )
    before = (led / "reflex.json").read_text(encoding="utf-8")
    assert resolver.session_state(tmp_path, "pytest tests").circuit == "dense"
    assert resolver.session_state(tmp_path, "go test").circuit == "bypass"
    assert resolver.session_state(tmp_path, "other").circuit == "normal"
    assert resolver.session_state(tmp_path, None).circuit == "normal"
    assert (led / "reflex.json").read_text(encoding="utf-8") == before  # read-only


def test_retrieval_budget_returns_current_values_exactly(tmp_path):
    from ctx import resolver
    from ctx.config import load_config

    cfg = load_config(None)
    assert resolver.resolve_retrieval_budget(cfg, None) == cfg.budgets.turn_retrieval_tokens
    assert resolver.resolve_retrieval_budget(cfg, None, requested=600) == 600
    assert resolver.resolve_retrieval_budget(object(), None) == 2800  # fail-open


# --------------------------------------------- golden byte-identity (CLI)
def _legacy_emitted(ws, text: str, exit_code: int) -> str:
    """The pre-change emission pipeline, verbatim: base choice by zero-hop
    marker, failure ×factor, engagement filter, bounded backstop."""
    from ctx.engagement import filter_digest, suggestion_cap
    from ctx.textutil import bounded

    budget = (
        ws.config.budgets.result_tokens
        if "output (complete):" in text
        else ws.config.budgets.digest_tokens
    )
    if exit_code != 0:
        budget = int(budget * ws.config.budgets.failure_budget_factor)
    eng = ws.config.engagement
    cap = suggestion_cap(ws.root, mode=eng.mode, lean_models=eng.lean_models)
    return bounded(filter_digest(text, cap), budget) + "\n"


@pytest.mark.parametrize("fail", [False, True])
def test_golden_run_digest_byte_identical_to_legacy(ws_store, capsys, fail):
    from ctx.cli import main
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    ws, store = ws_store
    lines = "".join(f"print('ERROR: item {i} broke')\n" for i in range(400))
    code = lines + ("import sys; sys.exit(3)\n" if fail else "")
    rc = main(["--workspace", str(ws.root), "run", "--", sys.executable, "-c", code])
    assert rc == (3 if fail else 0)
    emitted = capsys.readouterr().out

    # Independent legacy computation: replay mints the identical digest.
    cap2 = run_capture(ws, [sys.executable, "-c", code], store=store)
    digest, _ = render_run_digest(store, ws, cap2.manifest)
    assert emitted == _legacy_emitted(ws, digest, 3 if fail else 0)


@pytest.mark.parametrize("fail", [False, True])
def test_golden_eval_digest_byte_identical_to_legacy(ws_store, capsys, fail):
    from ctx.cli import main
    from ctx.pyeval import run_eval

    ws, store = ws_store
    script = "".join(f"print('ERROR: eval item {i}')\n" for i in range(400)) + (
        "raise SystemExit(2)\n" if fail else ""
    )
    rc = main(["--workspace", str(ws.root), "eval", script])
    assert rc == (3 if fail else 0)
    emitted = capsys.readouterr().out

    text, code = run_eval(ws, store, script)
    assert code == (2 if fail else 0)
    assert emitted == _legacy_emitted(ws, text, code)


@pytest.mark.parametrize("fail", [False, True])
def test_golden_seq_digest_byte_identical_to_legacy(ws_store, capsys, fail):
    from ctx.cli import main
    from ctx.seq import run_seq

    ws, store = ws_store
    steps = ["echo alpha", "sh -c 'exit 1'" if fail else "echo beta"]
    rc = main(["--workspace", str(ws.root), "seq", *steps])
    assert rc == (3 if fail else 0)
    emitted = capsys.readouterr().out

    text, code = run_seq(ws, store, steps, halt_on_fail=True, timeout=None, focus=None)
    assert code == (1 if fail else 0)
    # seq always emits against the result budget (pre-change behavior).
    from ctx.engagement import filter_digest, suggestion_cap
    from ctx.textutil import bounded

    budget = ws.config.budgets.result_tokens
    if code != 0:
        budget = int(budget * ws.config.budgets.failure_budget_factor)
    eng = ws.config.engagement
    cap = suggestion_cap(ws.root, mode=eng.mode, lean_models=eng.lean_models)
    assert emitted == bounded(filter_digest(text, cap), budget) + "\n"


# ------------------------------------------------------------- telemetry
def test_plan_receipt_appended_to_telemetry(ws_store, capsys):
    from ctx.cli import main

    ws, store = ws_store
    main(["--workspace", str(ws.root), "run", "--", sys.executable, "-c", "print('x')"])
    capsys.readouterr()
    events = [
        json.loads(line)
        for line in (store.audit_dir / "telemetry.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    plans = [ev for ev in events if ev.get("op") == "plan"]
    assert plans, "run emission must append a plan receipt"
    receipt = plans[-1]
    assert set(receipt) >= {"op", "plan_id", "mode", "reasons"}
    assert len(receipt["plan_id"]) == 12
    assert receipt["mode"] in ("pass_summary", "fail_census", "dense", "bypass", "flood")
    from ctx.resolver import REASON_VOCABULARY

    assert set(receipt["reasons"]) <= set(REASON_VOCABULARY)


# --------------------------------------------------- the seven-site audit
def test_no_hand_rolled_budget_math_left_in_cli():
    """Mechanical form of the module docstring's seven-site audit: cli.py
    no longer multiplies by failure_budget_factor or chooses digest-vs-
    result budgets outside the resolver, and every retrieval verb funnels
    through the single _emit_retrieval choke point."""
    from pathlib import Path

    import ctx.cli as cli_mod

    src = Path(cli_mod.__file__).read_text(encoding="utf-8")
    assert "failure_budget_factor" not in src  # budget math lives in resolver.py
    assert src.count("_emit_retrieval(ws, store, out)") == 4  # diff/map/code/retrieval
    assert src.count("_delivery_plan(") >= 3  # run + eval + seq (+ render plan)
    assert "resolve_retrieval_budget" in src


def test_dense_latch_selects_dense_mode_through_cli_state(ws_store):
    from ctx import resolver

    ws, _ = ws_store
    led = ws.root / ".ctx-session-reads"
    led.mkdir(exist_ok=True)
    (led / "reflex.json").write_text(
        json.dumps({"densify": {"pytest": True}}), encoding="utf-8"
    )
    plan = resolver.resolve_delivery(
        "failure",
        "run",
        contract_rendering={"base_tokens": 480},
        session=resolver.session_state(ws.root, "pytest"),
        environment=resolver.environment_signals(ws.root),
        config_budgets=ws.config.budgets,
    )
    assert plan.mode == "dense"
    assert plan.token_budget == 960  # dense changes rendering, not budget
