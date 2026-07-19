"""Smoke guard for the programmable-capture eval set (evals/evalset_collapse.py).

Runs the two fast scenarios end-to-end so the eval harness cannot rot
silently: the fixture builder, the arm ledger, and the scenario assertions
(which are the acceptance checks) all execute. The fan-out scenario and the
live A/B are excluded here for runtime; they run via the evals/ entrypoints.
"""

import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"


@pytest.fixture()
def evalset(monkeypatch, tmp_path):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    sys.path.insert(0, str(EVALS))
    try:
        import evalset_collapse

        yield evalset_collapse
    finally:
        sys.path.remove(str(EVALS))


def test_flood_needle_scenario(evalset, tmp_path):
    root = tmp_path / "fx1"
    evalset.build_fixture(root)
    arms, checks = evalset.scenario_flood_needle(root)
    assert arms[0].rounds == 1
    assert arms[0].entered < 4000  # ~100k raw stayed out of the ledger
    assert any("quiet needle" in c for c in checks)


def test_branch_scenario_arms_ordered(evalset, tmp_path):
    root = tmp_path / "fx2"
    evalset.build_fixture(root)
    arms, checks = evalset.scenario_branch(root)
    by_name = {a.name: a for a in arms}
    assert by_name["eval"].rounds == 1
    assert by_name["pipeline"].rounds == 1  # the honest control collapses too
    assert by_name["rounds"].rounds == 2
    assert any("importers" in c for c in checks)
