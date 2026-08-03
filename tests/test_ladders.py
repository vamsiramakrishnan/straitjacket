"""The ladder registry: one declaration, four consumers, no drift.

The nine escalation ladders lived in four places — a prose table in
`docs/LADDERS.md`, a hardcoded list in the diagram generator, the rung strings
in the code that emits them, and a hand-maintained "measured today?" column.
Four copies of one fact is the defect class this codebase keeps finding in its
own caches, and the hand-maintained column was the copy most likely to drift
into advertising, because nothing could contradict it.

These tests hold the registry to three properties: the audit column is
*derived*, configuration can only narrow, and a measurable-but-silent ladder
is never reported as a working one.
"""

from __future__ import annotations

import json

import pytest

from ctx import ladders as L


# ------------------------------------------------------------- the registry
def test_every_ladder_is_either_measurable_or_says_why_not():
    """"Not scored" must always come with a reason.

    An audit whose negative entries are blank is a feature list with gaps.
    """
    for lad in L.LADDERS:
        if lad.measurable:
            assert lad.signal.ledger and lad.signal.field
        else:
            assert lad.unmeasured_because.strip(), (
                f"{lad.key} declares no signal and no reason — say why it "
                "cannot be scored, or give it one"
            )


def test_keys_and_names_are_unique():
    assert len({lad.key for lad in L.LADDERS}) == len(L.LADDERS)
    assert len({lad.name for lad in L.LADDERS}) == len(L.LADDERS)


def test_every_ladder_has_at_least_two_rungs():
    """A one-rung ladder is not a ladder; it is a setting."""
    for lad in L.LADDERS:
        assert len(lad.rungs) >= 2, lad.key


def test_a_signal_never_maps_onto_a_rung_the_ladder_does_not_have():
    """The mapping is the part that rots.

    Emitting code names rungs for its own purposes; this registry names them
    for a reader. When those vocabularies drift, a mapping silently resolves
    to nothing and the ladder reports a confident histogram of zeros — which
    is exactly what the first draft of this registry did for three ladders.
    """
    for lad in L.LADDERS:
        if lad.signal is None or lad.signal.rung_of is None:
            continue
        for source, target in lad.signal.rung_of.items():
            assert target in lad.rungs, (
                f"{lad.key}: signal maps {source!r} onto {target!r}, which is "
                f"not one of its rungs {list(lad.rungs)}"
            )


# ------------------------------------------------------------ configuration
def test_configuration_narrows_a_ladder():
    got = L.configured({"capture": {"rungs": ["native read", "run", "seq"]}})
    capture = {lad.key: lad for lad in got}["capture"]
    assert capture.rungs == ("native read", "run", "seq")


def test_configuration_preserves_declared_order():
    """Rungs are an escalation ORDER, not a set. Honouring the user's listing
    order would let a config invert the ladder."""
    got = L.configured({"capture": {"rungs": ["seq", "native read", "run"]}})
    capture = {lad.key: lad for lad in got}["capture"]
    assert capture.rungs == ("native read", "run", "seq")


def test_configuration_cannot_invent_a_rung():
    """A rung is a code path. Declaring one nothing implements would produce a
    report about a ladder that does not exist."""
    got = L.configured({"solution": {"rungs": ["not needed", "teleportation"]}})
    solution = {lad.key: lad for lad in got}["solution"]
    assert "teleportation" not in solution.rungs
    problems = L.validate({"solution": {"rungs": ["not needed", "teleportation"]}})
    assert any("teleportation" in p for p in problems), (
        "an invented rung was dropped SILENTLY — declared omission, not quiet"
    )


def test_an_unknown_ladder_is_reported_not_ignored():
    problems = L.validate({"bogus": {"rungs": ["x"]}})
    assert any("bogus" in p for p in problems)


def test_a_selection_matching_nothing_leaves_the_ladder_intact():
    """Fail-open: a typo'd config must not silently disable a ladder."""
    got = L.configured({"capture": {"rungs": ["nonsense"]}})
    capture = {lad.key: lad for lad in got}["capture"]
    assert capture.rungs == L.BY_KEY["capture"].rungs
    assert any("selects nothing" in p for p in L.validate(
        {"capture": {"rungs": ["nonsense"]}}))


@pytest.mark.parametrize("bad", [
    {"capture": "not-a-table"},
    {"capture": {"rungs": "native read"}},
])
def test_malformed_config_is_reported_and_survivable(bad):
    """ctx.toml is foreign input; a wrong-typed value must not raise."""
    L.configured(bad)                      # must not raise
    assert L.validate(bad), "malformed config produced no problem report"


# ------------------------------------------------------------- measurement
def test_an_unmeasurable_ladder_reports_its_reason(tmp_path):
    m = L.measure(tmp_path, L.BY_KEY["guard"])
    assert m["measurable"] is False
    assert m["reason"]
    assert m["rungs"] == {}


def test_measurable_but_silent_is_distinct_from_unmeasured(tmp_path):
    """"The instrument exists and saw nothing" and "there is no instrument"
    are different facts. Collapsing them is how a dry ladder starts looking
    like a working one."""
    silent = L.measure(tmp_path, L.BY_KEY["capture"])   # no ledger in tmp_path
    assert silent["measurable"] is True
    assert silent["records"] == 0

    unscored = L.measure(tmp_path, L.BY_KEY["solution"])
    assert unscored["measurable"] is False


def test_measurement_reads_real_rung_records(tmp_path):
    from ctx.sessiondir import LEDGER_DIR_NAME

    ledger = tmp_path / LEDGER_DIR_NAME
    ledger.mkdir(parents=True)
    (ledger / "collapse.jsonl").write_text(
        "\n".join(
            json.dumps({"op": "collapse", "rung": r, "shape": "x"})
            for r in ("reuse-index", "reuse-index", "bounded-search")
        ),
        encoding="utf-8",
    )
    m = L.measure(tmp_path, L.BY_KEY["capture"])
    assert m["records"] == 3
    assert m["rungs"]["native read"] == 2   # reuse-index maps here
    assert m["rungs"]["run"] == 1           # bounded-search maps here
    assert m["unmapped"] == 0


def test_an_unmappable_value_is_declared_not_dropped(tmp_path):
    """The declared-omission rule, applied to telemetry."""
    from ctx.sessiondir import LEDGER_DIR_NAME

    ledger = tmp_path / LEDGER_DIR_NAME
    ledger.mkdir(parents=True)
    (ledger / "collapse.jsonl").write_text(
        json.dumps({"op": "collapse", "rung": "a-rung-from-the-future"}),
        encoding="utf-8",
    )
    m = L.measure(tmp_path, L.BY_KEY["capture"])
    assert m["records"] == 1
    assert m["unmapped"] == 1
    assert sum(m["rungs"].values()) == 0


def test_a_corrupt_ledger_fails_open(tmp_path):
    from ctx.sessiondir import LEDGER_DIR_NAME

    ledger = tmp_path / LEDGER_DIR_NAME
    ledger.mkdir(parents=True)
    (ledger / "collapse.jsonl").write_text("{not json\n{\"rung\": \"reuse-index\"}\n")
    m = L.measure(tmp_path, L.BY_KEY["capture"])
    assert m["records"] == 1, "a bad line must not discard the good ones"


def test_window_pressure_buckets_a_number_onto_its_rungs(tmp_path):
    from ctx.sessiondir import LEDGER_DIR_NAME

    proxy = tmp_path / LEDGER_DIR_NAME / "proxy"
    proxy.mkdir(parents=True)
    (proxy / "window.json").write_text(json.dumps({"window_pct": 91.0}))
    m = L.measure(tmp_path, L.BY_KEY["pressure"])
    assert m["rungs"]["84%"] == 1


# ------------------------------------------------------------ the consumers
def test_the_diagram_generator_reads_the_registry():
    """Not a copy of it.

    A diagram drawn from its own list is a fourth place the truth can live.
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "scripts"))
    import gen_ladders_diagram as gen

    assert gen.REGISTRY is L.LADDERS
    drawn = [name for name, _, _, _ in gen.rows()]
    assert drawn == [lad.name for lad in L.LADDERS]


def test_the_report_names_every_ladder(tmp_path):
    text = L.report(tmp_path)
    for lad in L.LADDERS:
        assert lad.name in text


def test_the_report_summary_counts_add_up(tmp_path):
    text = L.report(tmp_path)
    tail = text.strip().splitlines()[-1]
    measured = sum(1 for lad in L.LADDERS if lad.measurable)
    # In an empty workspace every measurable ladder is silent.
    assert f"0 measured · {measured} instrumented but silent" in tail
    assert f"{len(L.LADDERS) - measured} not scored" in tail


def test_the_audit_doc_documents_every_ladder_in_the_registry():
    """`docs/LADDERS.md` is CHECKED against the registry, not generated from it.

    Generating it was the obvious move and is the wrong one: each row carries
    prose a table cannot hold (signal, latching rationale, what it would take
    to make the ladder measurable), and generating would either lose that or
    push it into the registry where it does not belong. So the doc stays
    hand-written and this test holds the one property that actually drifts —
    a ladder added to the code and never written down.
    """
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "docs" / "LADDERS.md").read_text(
        encoding="utf-8"
    )
    rows = [
        line.split("|")[1].strip()
        for line in doc.splitlines()
        if line.startswith("| ") and line.count("|") >= 7
    ]
    rows = [r for r in rows if r not in ("ladder", "---")]
    assert len(rows) == len(L.LADDERS), (
        f"the audit table has {len(rows)} rows for {len(L.LADDERS)} ladders — "
        "a ladder was added to ctx.ladders and never documented (or vice versa)"
    )
    for lad in L.LADDERS:
        assert any(lad.name in row for row in rows), (
            f"{lad.name!r} is in the registry but has no row in docs/LADDERS.md"
        )
