"""Acceptance: scorecard v2 (EDC §20) and the seeded frozen referee
(EDC §19 / phase 0, debt 34e21fe2dc).

Scorecard v2 folds the NEW intervention ledger
(``.ctx-session-reads/interventions.jsonl`` — ctx.intervention/v1 emission
lines + ctx.intervention-outcome/v1 outcome lines) into per-family
behavioral blocks, labeled downstream-cost estimates, an evidence-coverage
table, and per-signature episode narratives. Absent/empty/corrupt v2
ledger → the v1 rendering stays byte-identical. ``expired_unresolved`` is
a censored observation: excluded from every rate denominator.

The runner side: ``aggregate_rows`` (medians/min/max per task×arm across
repeats) and ``evaluate_gates`` (EDC §19.2 economic gates on medians — the
round-3 variance-wall lesson) are pure functions tested with synthetic
rows; the frozen-referee constants are guarded by a recorded sha256 so any
drift fails loudly. No live sessions run here.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

# ---------------------------------------------------------------- fixtures


def _make_session(tmp_path, name="reads"):
    """A .ctx-session-reads-shaped dir: proxy/wire.jsonl inside, ledgers
    beside it. Returns (session_reads_dir, proxy_dir)."""
    reads = tmp_path / name
    proxy = reads / "proxy"
    proxy.mkdir(parents=True)
    rec = {
        "seq": 1,
        "path": "/v1/messages",
        "status": 200,
        "messages": 4,
        "model": "claude-haiku-4-5",
        "tools": {"Bash": 1},
        "usage": {
            "input_tokens": 2,
            "output_tokens": 100,
            "cache_read_input_tokens": 50_000,
            "cache_creation_input_tokens": 400,
        },
        "ms": {"connect": 0.0, "ttfb": 1000.0, "total": 2000.0},
    }
    (proxy / "wire.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return reads, proxy


def _emit(iid, seq, family, sig, gen, mode, cov, hints, ts=1.0):
    """A frozen-schema ctx.intervention/v1 emission line."""
    return {
        "schema": "ctx.intervention/v1",
        "event": "intervention_emitted",
        "interventionId": iid,
        "sessionSeq": seq,
        "family": family,
        "signature": sig,
        "generation": gen,
        "artifact": "run:abc123",
        "planId": "plan-1",
        "planMode": mode,
        "coverage": cov,
        "hints": hints,
        "ts": ts,
    }


def _outcome(iid, outcome, ts=2.0, evidence="span:1"):
    """A frozen-schema ctx.intervention-outcome/v1 line."""
    return {
        "schema": "ctx.intervention-outcome/v1",
        "event": "intervention_outcome",
        "interventionId": iid,
        "outcome": outcome,
        "evidence": evidence,
        "ts": ts,
    }


def _write_v2(reads, lines):
    (reads / "interventions.jsonl").write_text(
        "".join(json.dumps(ln) + "\n" for ln in lines), encoding="utf-8"
    )


_COV_FULL = {"requiredFraction": 1.0, "named": [8, 8], "addressable": [8, 8]}


def _seed_full_fixture(reads):
    """The canonical v2 fixture: a pytest episode that starves, adapts to
    dense, and lands; a narrowed second signature; a censored lint one."""
    _write_v2(reads, [
        _emit("i1", 12, "pytest", "python -m pytest", 3, "fail_census",
              _COV_FULL, 2, ts=1.0),
        _outcome("i1", "equivalent_rerun", ts=2.0),
        {"schema": "ctx.circuit/v1", "event": "circuit_transition",
         "family": "pytest", "to": "dense", "ts": 2.5},
        _emit("i2", 15, "pytest", "python -m pytest", 3, "dense",
              _COV_FULL, 2, ts=3.0),
        _outcome("i2", "retrieval_landing", ts=4.0,
                 evidence={"runtime_ms": 1700}),
        _emit("i3", 21, "pytest", "pytest tests/test_x.py", 4, "dense",
              {"requiredFraction": 1.0, "named": [3, 3],
               "addressable": [3, 3]}, 1, ts=5.0),
        _outcome("i3", "progressed_without_retrieval", ts=6.0,
                 evidence={"runtime_ms": 800}),
        _emit("i4", 30, "lint", "ruff check .", 4, "fail_census",
              {"requiredFraction": 0.5, "named": [2, 4],
               "addressable": [4, 4]}, 0, ts=7.0),
        _outcome("i4", "expired_unresolved", ts=8.0),
    ])


# --------------------------------------------- v2 per-family blocks (§20)
def test_v2_family_counts(tmp_path):
    from ctx.scorecard import compute_scorecard

    reads, proxy = _make_session(tmp_path)
    _seed_full_fixture(reads)
    iv = compute_scorecard(proxy)["interventions"]
    py = iv["families"]["pytest"]
    assert py["events"] == 3
    assert py["census_complete"] == 3  # coverage.requiredFraction == 1.0
    assert py["hinted"] == 3  # retrieval opportunities: hints > 0
    assert py["landings"] == 1
    assert py["progressed"] == 1
    assert py["equivalent_reruns"] == 1
    assert py["slicer_reruns"] == 0
    assert py["expired"] == 0
    assert py["transitions"] == {"dense": 1}
    lint = iv["families"]["lint"]
    assert lint["events"] == 1
    assert lint["census_complete"] == 0  # requiredFraction 0.5 ≠ complete
    assert lint["hinted"] == 0
    assert lint["expired"] == 1


def test_v2_rendered_blocks_exact(tmp_path):
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    _seed_full_fixture(reads)
    text = render_scorecard(compute_scorecard(proxy))
    for expected in [
        "  interventions (v2 ledger):",
        "    pytest: 3 interventions · census complete 3/3 · hinted 3",
        "      outcomes: landings 1 · progressed w/o retrieval 1 "
        "· equivalent reruns 1 · slicer reruns 0",
        "      retrieval landing rate: 1/3 resolved opportunities",
        "      transitions: dense×1",
        "      censored: 1 expired_unresolved "
        "(excluded from all rate denominators)",
        "  evidence coverage:",
        "    family        events  required%    named  addressable",
        "    lint               1       50.0      2/4          4/4",
        "    pytest             3      100.0    19/19        19/19",
        "  episodes:",
        "    'python -m pytest': first at seq 12 (gen 3) · "
        "response fail_census→dense · "
        "outcomes equivalent_rerun → retrieval_landing",
        "    'ruff check .': first at seq 30 (gen 4) · "
        "response fail_census · outcomes expired_unresolved",
    ]:
        assert expected in text.splitlines(), expected


# --------------------------- downstream-cost estimates: labeled, formulaic
def test_v2_estimates_values_and_labels(tmp_path):
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    _seed_full_fixture(reads)
    sc = compute_scorecard(proxy)
    est = sc["interventions"]["estimates"]
    # i2 (dense → retrieval_landing) and i3 (dense → progressed) avoided a
    # rerun each; i1 (fail_census) and i4 (expired) never count.
    assert est["avoided_reexecutions"] == 2
    assert est["avoided_turns"] == 2
    # runtime: median observed same-signature runtimes 1.7s + 0.8s.
    assert est["avoided_runtime_s"] == 2.5
    text = render_scorecard(sc)
    assert "  estimated downstream cost (labeled estimates):" in text
    assert "    avoided reexecutions (estimate): 2" in text.splitlines()
    assert "    avoided turns (estimate): 2" in text.splitlines()
    assert "    avoided runtime (estimate): 2.5s" in text.splitlines()
    # Every counterfactual carries its derivation formula (EDC §20 binding
    # amendment): the trailing note names each metric's formula.
    note = next(ln for ln in text.splitlines() if ln.startswith("    note: "))
    assert "avoided reexecutions = adapted-plan (dense/bypass) " in note
    assert "resolved without an equivalent/slicer rerun" in note
    assert "avoided turns = same count" in note
    assert "median observed same-signature runtimes" in note
    assert "censored" in note


def test_v2_runtime_omitted_when_unobserved(tmp_path):
    """NEVER invent numbers: without runtime evidence in the events, the
    avoided-runtime metric is absent — not zero, not guessed."""
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    _write_v2(reads, [
        _emit("a1", 5, "pytest", "pytest", 1, "dense", _COV_FULL, 1),
        _outcome("a1", "retrieval_landing"),  # no runtime anywhere
    ])
    sc = compute_scorecard(proxy)
    est = sc["interventions"]["estimates"]
    assert est == {"avoided_reexecutions": 1, "avoided_turns": 1}
    text = render_scorecard(sc)
    assert "avoided reexecutions (estimate): 1" in text
    assert "avoided runtime (estimate)" not in text  # note formula only


def test_v2_no_estimates_block_without_adapted_plans(tmp_path):
    """No adaptation happened → the counterfactual has no basis; the whole
    downstream-cost block is omitted (inputs absent, metric absent)."""
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    _write_v2(reads, [
        _emit("b1", 3, "pytest", "pytest", 1, "fail_census", _COV_FULL, 1),
        _outcome("b1", "retrieval_landing"),
    ])
    sc = compute_scorecard(proxy)
    assert "estimates" not in sc["interventions"]
    assert "estimated downstream cost" not in render_scorecard(sc)


# ----------------------------------------------------- censoring (EDC §9)
def test_v2_expired_censored_from_rate_denominators(tmp_path):
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    _write_v2(reads, [
        _emit("c1", 4, "pytest", "pytest -x", 1, "dense", _COV_FULL, 2),
        _outcome("c1", "retrieval_landing", evidence={"runtime_ms": 500}),
        # hinted but expired: a censored observation — silence must not
        # train pessimism, so it leaves the landing-rate denominator.
        _emit("c2", 9, "pytest", "pytest -k slow", 1, "dense", _COV_FULL, 3),
        _outcome("c2", "expired_unresolved"),
    ])
    sc = compute_scorecard(proxy)
    py = sc["interventions"]["families"]["pytest"]
    assert py["hinted"] == 2
    assert py["hinted_resolved"] == 1  # c2 censored, not a denominator
    assert py["hinted_landed"] == 1
    assert py["expired"] == 1
    text = render_scorecard(sc)
    assert "      retrieval landing rate: 1/1 resolved opportunities" in (
        text.splitlines()
    )
    # c2 (dense, expired) is censored — never an avoided reexecution.
    assert sc["interventions"]["estimates"]["avoided_reexecutions"] == 1


# ------------------------------------------- v1-only byte-compatibility
def _v1_event(event, signature, action="none"):
    return {"ts": 1000.0, "event": event, "signature": signature,
            "run": None, "action": action}


def test_v1_only_rendering_byte_compatible(tmp_path):
    """Missing/empty/corrupt v2 ledger → exactly today's v1 block."""
    from ctx.scorecard import compute_scorecard, render_scorecard

    sessions = {}
    for name in ("absent", "empty", "corrupt"):
        reads, proxy = _make_session(tmp_path, name)
        (reads / "reflex-outcomes.jsonl").write_text(
            "".join(json.dumps(_v1_event("starvation", "python -m pytest",
                                         "densify" if i == 7 else "none"))
                    + "\n" for i in range(8)),
            encoding="utf-8",
        )
        sessions[name] = (reads, proxy)
    (sessions["empty"][0] / "interventions.jsonl").write_text("")
    (sessions["corrupt"][0] / "interventions.jsonl").write_bytes(
        b"\x00\xff not { json\n" + b'"a bare string"\n'
    )
    renders = {}
    for name, (_, proxy) in sessions.items():
        sc = compute_scorecard(proxy)
        assert "interventions" not in sc
        renders[name] = render_scorecard(sc)
    assert renders["absent"] == renders["empty"] == renders["corrupt"]
    # ...and it is exactly today's v1 anomalies block, nothing more.
    assert (
        "  anomalies: 8 starvation (1 signature: 'python -m pytest') "
        "· 0 landings · densified: yes" in renders["absent"].splitlines()
    )
    assert "interventions" not in renders["absent"]
    assert "evidence coverage" not in renders["absent"]


def test_v2_corrupt_lines_skipped_good_lines_counted(tmp_path):
    from ctx.scorecard import compute_scorecard

    reads, proxy = _make_session(tmp_path)
    good = _emit("g1", 2, "pytest", "pytest", 1, "fail_census", _COV_FULL, 1)
    (reads / "interventions.jsonl").write_text(
        "not { json at all\n"
        + json.dumps(good) + "\n"
        + '"bare string"\n'
        + json.dumps(_outcome("g1", "verbatim_retry")) + "\n"
        + json.dumps(_outcome("ghost", "retrieval_landing")) + "\n"  # unattributable
        + json.dumps({"event": "wormhole"}) + "\n",  # unknown event kind
        encoding="utf-8",
    )
    iv = compute_scorecard(proxy)["interventions"]
    py = iv["families"]["pytest"]
    assert py["events"] == 1
    assert py["verbatim_retries"] == 1
    assert py["landings"] == 0  # the ghost outcome attributed nowhere


def test_v2_unknown_outcome_tolerated_as_other(tmp_path):
    """Schema-v2 discipline (EDC §5.6): unknown outcome kinds → 'other',
    never an error, never a counted known bucket."""
    from ctx.scorecard import compute_scorecard

    reads, proxy = _make_session(tmp_path)
    _write_v2(reads, [
        _emit("u1", 2, "pytest", "pytest", 1, "fail_census", _COV_FULL, 0),
        _outcome("u1", "quantum_recovery"),
    ])
    py = compute_scorecard(proxy)["interventions"]["families"]["pytest"]
    assert py["other_outcomes"] == 1
    assert py["landings"] == 0


def test_v2_coverage_fields_missing_render_dashes(tmp_path):
    """Coverage cells with absent inputs render as absent (—), never 0."""
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    _write_v2(reads, [
        _emit("m1", 2, "sh", "make build", 1, "fail_census", {}, 0),
    ])
    sc = compute_scorecard(proxy)
    fam = sc["interventions"]["families"]["sh"]
    assert fam["required_pct"] is None
    assert fam["named"] is None and fam["addressable"] is None
    line = next(
        ln for ln in render_scorecard(sc).splitlines()
        if ln.startswith("    sh ")
    )
    assert line.split() == ["sh", "1", "—", "—", "—"]


# =================================================== spec3 runner (phase 0)
FROZEN_REFEREE_SHA256 = (
    "ba78952f3d2441b41ffdda9e5bc6e554683f757eba086581218fde1c651209b4"
)


def test_frozen_referee_constants():
    """The frozen-referee contract (debt 34e21fe2dc): seed support was
    added WITHOUT changing one byte of the task prompt, specs, holdout
    suites, or arm construction. Any drift invalidates every cross-round
    comparison in evals/spec3-haiku-2026-07-18.md — so it fails loudly
    here, and the fix is to revert the drift, not to update the hash."""
    import spec3_runner as sr

    h = hashlib.sha256()
    h.update(sr.TASK_PROMPT.encode())
    h.update(json.dumps(sr.SPECS, sort_keys=True).encode())
    h.update(json.dumps(sr.HOLDOUT, sort_keys=True).encode())
    for arm in ("naive", "sj", "headroom"):  # arm construction, incl.
        h.update(json.dumps(sr.arm_argv(arm, "haiku")).encode())  # tools/caps
    assert h.hexdigest() == FROZEN_REFEREE_SHA256, (
        "spec3 frozen-referee constants drifted — TASK_PROMPT/SPECS/"
        "HOLDOUT/arm_argv must not change (cross-round comparisons die)."
    )


def _row(task, arm, rep, turns, cost, wall, cache, holdout_frac=1.0,
         holdout="9/9"):
    return {"task": task, "arm": arm, "rep": rep, "turns": turns,
            "cost_usd": cost, "wall_s": wall, "cache_hit_pct": cache,
            "holdout_frac": holdout_frac, "holdout": holdout}


def _three_reps(task, arm, turns3, cost3, wall3, cache3, fracs=(1.0,) * 3):
    return [
        _row(task, arm, i + 1, turns3[i], cost3[i], wall3[i], cache3[i],
             holdout_frac=fracs[i],
             holdout="9/9" if fracs[i] == 1.0 else "8/9")
        for i in range(3)
    ]


def test_aggregate_rows_medians_math():
    from spec3_runner import aggregate_rows

    rows = (
        _three_reps("tokenbucket", "naive", (26, 13, 19),
                    (0.278, 0.114, 0.179), (159.5, 80.0, 120.0),
                    (95.2, 96.0, 96.6))
        + _three_reps("tokenbucket", "sj", (33, 25, 28),
                      (0.371, 0.260, 0.300), (202.0, 150.0, 175.0),
                      (96.3, 98.0, 98.2))
    )
    m = aggregate_rows(rows)
    nv = m["tokenbucket/naive"]
    assert nv["n"] == 3
    assert nv["turns"] == {"median": 19, "min": 13, "max": 26}
    assert nv["cost_usd"]["median"] == 0.179
    assert nv["cache_hit_pct"] == {"median": 96.0, "min": 95.2, "max": 96.6}
    sj = m["tokenbucket/sj"]
    assert sj["turns"]["median"] == 28
    assert sj["wall_s"] == {"median": 175.0, "min": 150.0, "max": 202.0}


def test_aggregate_rows_even_count_and_none_skipped():
    from spec3_runner import aggregate_rows

    rows = [
        _row("csvq", "naive", 1, 10, 0.1, 60.0, 96.0),
        _row("csvq", "naive", 2, 20, 0.2, 80.0, 97.0),
        # failed session: turns None must be skipped, not crash the median
        _row("csvq", "naive", 3, None, 0.0, 30.0, None),
    ]
    m = aggregate_rows(rows)["csvq/naive"]
    assert m["n"] == 3
    assert m["turns"] == {"median": 15, "min": 10, "max": 20}  # even count
    assert m["cache_hit_pct"] == {"median": 96.5, "min": 96.0, "max": 97.0}
    # a metric with zero numeric observations is omitted entirely
    assert "turns" not in aggregate_rows(
        [_row("csvq", "sj", 1, None, 0.0, 1.0, None)]
    )["csvq/sj"]


def test_gates_pass_path():
    from spec3_runner import aggregate_rows, evaluate_gates

    rows = (
        _three_reps("tokenbucket", "naive", (13, 19, 26),
                    (0.1, 0.15, 0.2), (60, 90, 120), (95.2, 96.0, 96.6))
        + _three_reps("tokenbucket", "sj", (21, 25, 28),
                      (0.2, 0.25, 0.3), (90, 120, 150), (96.3, 98.0, 98.2))
    )
    gates, ok = evaluate_gates(rows, aggregate_rows(rows))
    assert ok is True
    by = {g["gate"]: g for g in gates}
    # sj median 25 <= 1.5 x naive median 19 = 28.5
    g = by["turns_ratio[tokenbucket]"]
    assert g["ok"] and "sj median 25" in g["detail"] and "28.5" in g["detail"]
    g = by["cache_advantage[tokenbucket]"]
    assert g["ok"] and "98.0" in g["detail"] and "96.0" in g["detail"]
    g = by["holdout_all_pass"]
    assert g["ok"] and "6/6 rows" in g["detail"]


def test_gates_fail_paths_and_exit_semantics():
    from spec3_runner import aggregate_rows, evaluate_gates

    rows = (
        _three_reps("tokenbucket", "naive", (13, 19, 26),
                    (0.1, 0.15, 0.2), (60, 90, 120), (97.0, 96.0, 96.6))
        + _three_reps("tokenbucket", "sj", (33, 33, 33),
                      (0.3, 0.35, 0.4), (150, 180, 200), (95.0, 95.5, 94.0),
                      fracs=(1.0, 0.889, 1.0))
    )
    gates, ok = evaluate_gates(rows, aggregate_rows(rows))
    assert ok is False
    by = {g["gate"]: g for g in gates}
    assert not by["turns_ratio[tokenbucket]"]["ok"]  # 33 > 28.5
    assert not by["cache_advantage[tokenbucket]"]["ok"]  # 95.0 < 96.6
    g = by["holdout_all_pass"]
    assert not g["ok"]
    assert "5/6 rows" in g["detail"]
    assert "tokenbucket/sj rep2 8/9" in g["detail"]


def test_gates_missing_arm_fails_closed():
    from spec3_runner import aggregate_rows, evaluate_gates

    rows = _three_reps("csvq", "naive", (11, 11, 19), (0.08, 0.09, 0.17),
                       (66, 70, 100), (96.7, 96.5, 96.8))
    gates, ok = evaluate_gates(rows, aggregate_rows(rows))
    assert ok is False  # a gate that cannot see its numbers must not pass
    by = {g["gate"]: g for g in gates}
    assert not by["turns_ratio[csvq]"]["ok"]
    assert "FAIL closed" in by["turns_ratio[csvq]"]["detail"]
    assert not by["cache_advantage[csvq]"]["ok"]
    assert by["holdout_all_pass"]["ok"]  # holdout itself was fine
