"""Smoke guard for the plan-collapse eval (evals/plan_collapse.py).

Runs the three arms end-to-end on the seeded auth-regression fixture so the
measurement harness cannot rot silently: the fixture builder, all three arm
runners, the cost formulas, and the cache probe execute. Numbers vary a few
bytes run to run (real pytest/git output), so assertions are on structure
and ordering, never exact byte counts.

Engine-agnostic by construction: arm P's ast.search node degrades to the
anchored-rg / stdlib fallback when ast-grep is absent, so this passes on a
bare [dev] install with zero optional engines.
"""

import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"


@pytest.fixture()
def plancol(monkeypatch, tmp_path):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    sys.path.insert(0, str(EVALS))
    try:
        import plan_collapse

        yield plan_collapse
    finally:
        sys.path.remove(str(EVALS))


def test_arms_shape_and_cost_ordering(plancol, tmp_path):
    root = tmp_path / "fx"
    root.mkdir()
    plancol.build_fixture(root)

    n = plancol.arm_naive(root)
    b = plancol.arm_harness(root)
    p, plan_json = plancol.arm_plan(root)

    # Boundary crossings collapse 6 → 4 → 1.
    assert len(n) == 6
    assert len(b) == 4
    assert len(p) == 1

    # The compiled plan is the cheapest arm on both first-exposure and the
    # latency-weighted resend cost; one crossing means C == O_1.
    fe = {k: plancol.first_exposure(v) for k, v in (("n", n), ("b", b), ("p", p))}
    rc = {k: plancol.resend_cost(v) for k, v in (("n", n), ("b", b), ("p", p))}
    assert fe["p"] < fe["n"] and fe["p"] < fe["b"]
    assert rc["p"] < rc["b"] < rc["n"]
    assert rc["p"] == plancol.nbytes(p[0][1])  # R=1 ⇒ C = 1·O_1

    # The plan JSON is real model-authored output, non-trivial.
    assert plancol.nbytes(plan_json) > 0

    # The single investigation digest carries the conclusion, not a log dump.
    digest = p[0][1]
    assert "profile=investigate/v1" in digest
    assert "conclusion candidates" in digest
    assert "from_request" in digest and "ValueError" in digest
    assert "counterevidence:" in digest and "coverage:" in digest


def test_cache_probe_reads_are_byte_stable(plancol, tmp_path):
    # Pure reads (search/get) are content-addressed → byte-identical on an
    # unchanged worktree; the row-shape contract holds regardless.
    rows = plancol.cache_probe(plancol.build_fixture, plancol.arm_harness)
    assert len(rows) == 4
    by_step = {label: (ident, cause) for label, ident, cause in rows}
    assert by_step["ctx search repo: from_request"][0] is True
    assert by_step["ctx get repo:auth.py --symbol from_request"][0] is True


def test_report_renders_all_sections(plancol):
    md = plancol.run_all()
    assert "C = Σ i·O_i" in md
    assert "Per-step first-exposure bytes" in md
    assert "Cache-stability" in md
