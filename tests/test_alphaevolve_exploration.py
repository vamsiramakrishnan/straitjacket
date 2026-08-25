from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.alphaevolve import evaluate as ev_mod

FIXTURES = Path(ev_mod.__file__).resolve().parent / "fixtures" / "exploration"


def test_exploration_corpus_is_frozen_and_loadable():
    index = json.loads((FIXTURES / "index.json").read_text())
    assert index["schema"] == "ctx.alphaevolve-exploration-corpus/v1"
    assert len(index["cases"]) >= 4
    cases = ev_mod._exploration_cases()
    by_name = {case["name"]: case for case in cases}
    # The grep fixture is a genuinely large exploration output.
    assert len(by_name["grep_sweep_locate_resolver"]["lines"]) > 700
    # Every graded index is inside its case and grades are in {1,2,3}.
    for case in cases:
        n = len(case["lines"])
        for idx, grade in case["relevance"].items():
            assert 0 <= idx < n
            assert grade in (1, 2, 3)


def test_seed_passes_all_gates_validity_and_exploration_runs():
    seed = ev_mod.INITIAL_PROGRAM_CODE
    for fn in (
        ev_mod.score_candidate,
        ev_mod.score_holdout,
        ev_mod.score_adversarial,
        ev_mod.score_exploration,
    ):
        result = fn(seed)
        assert result["error"] is None, result
        assert 0 < result["score"] <= 100


def test_translated_text_profile_ranks_decisive_lines_above_traps():
    from ctx.digest.text import _signal_rank

    decisive = _signal_rank("src/ctx/workspace.py:398:def resolve_workspace(")
    benign = _signal_rank("no errors detected during preflight")
    keywordless_status = _signal_rank("HTTP/1.1 502 Bad Gateway")
    trap_inside_quotes = _signal_rank('msg "verified, 0 errors" confirmed')
    assert decisive > 0
    assert benign == -1
    assert keywordless_status == 3
    assert trap_inside_quotes == -1


def test_exploration_corpus_stays_out_of_managed_search():
    """The search corpus must not grow into the exploration fixtures."""
    assert all("exploration" not in str(case.get("name", "")).lower() for case in ev_mod._CASES)
