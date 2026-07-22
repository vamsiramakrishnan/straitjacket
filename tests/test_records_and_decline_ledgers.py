"""Acceptance: the two M-K demand ledgers.

M-K3 `records_opportunity` — a jq / sort|uniq -c / awk-projection pipeline
that hits the guard is the demand denominator for `ctx q records`; the
hook detects the shape, appends a ledger line, and teaches the collapse.

M-K5 `comby_candidate` — an `ast.rewrite.preview` that the ast-grep rung
cannot express (engine absent, or a pattern that matched nothing) is
recorded into the decline corpus that gates the comby rung. Instrument
only — the rung is NOT built (docs/SUBSTRATE.md §M-K5)."""

from __future__ import annotations

import json

import pytest

from conftest import make_store, make_ws


# ------------------------------------------------ M-K3 records_opportunity
def test_records_opportunity_detects_transform_shapes():
    from ctx.hook import _records_opportunity

    for cmd in (
        "cat run.json | jq '.results[]'",
        "jq -r '.[].name' data.json",
        "sort names.txt | uniq -c",
        "cut -f2 x.tsv | sort | uniq -c | sort -rn",
        "awk '{print $2}' access.log",
        "gawk '{ print $1 }' f",
    ):
        assert _records_opportunity(cmd), cmd


def test_records_opportunity_negatives():
    from ctx.hook import _records_opportunity

    for cmd in (
        "sort names.txt",          # sort alone is not a group-count
        "awk 'END{print NR}' f",   # no field projection
        "grep foo bar.txt",
        "ctx q 'records run:x --jsonl | group level | count'",  # already collapsed
        "jქ",                      # not jq
        "",
    ):
        assert not _records_opportunity(cmd), cmd


def test_records_opportunity_ledgers_and_teaches(tmp_path, monkeypatch):
    from ctx.hook import classify

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    d = classify({
        "tool_name": "run_command",
        "tool_input": {"CommandLine": "cat out.json | jq '.results[]'",
                       "Cwd": str(tmp_path)},
        "workspacePaths": [str(tmp_path)],
    })
    # A jq pipeline is a compound expression → force_ask; the records teach
    # rides the reason, and the ledger records the opportunity.
    assert "ctx q 'records" in d.get("reason", "")
    ledger = tmp_path / ".ctx-session-reads" / "records-adoption.jsonl"
    assert ledger.is_file()
    ev = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert ev["op"] == "records_opportunity" and ev["taught"] is True


# ------------------------------------------------- M-K5 comby_candidate
def test_rewrite_decline_records_comby_candidate(state_home, workspace_dir):
    """A structural rewrite that matches nothing is logged to the decline
    corpus — the demand denominator for the (unbuilt) comby rung."""
    astgrep = pytest.importorskip("ctx.astgrep")
    if not astgrep.available():
        pytest.skip("ast-grep not installed")
    from ctx.plan_ops import OPS, PlanContext

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "m.py").write_text("x = 1\n", encoding="utf-8")
    pc = PlanContext(ws=ws, store=store)
    # A pattern that cannot match this file → no_structural_match decline.
    OPS["ast.rewrite.preview"].fn(
        pc, {"pattern": "nonexistent_call($A)", "rewrite": "renamed($A)",
             "language": "python"}, None,
    )
    ledger = workspace_dir / ".ctx-session-reads" / "rewrite-declines.jsonl"
    assert ledger.is_file()
    ev = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert ev["op"] == "comby_candidate"
    assert ev["reason"] == "no_structural_match"
    assert ev["pattern"] == "nonexistent_call($A)"


def test_rewrite_decline_ledger_is_generation_excluded(state_home, workspace_dir):
    """The ledger dir is bookkeeping — writing it must not perturb the
    generation hash (house rule). As in a real ctx workspace, the dir is
    gitignored, so it never enters git porcelain and the generation is
    stable across ledger writes."""
    import subprocess

    from ctx.execution import generation_hash
    from ctx.plan_ops import PlanContext, _note_rewrite_decline

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    subprocess.run(["git", "init", "-q"], cwd=workspace_dir, check=True)
    (workspace_dir / ".gitignore").write_text(".ctx-session-reads/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=workspace_dir, check=True)
    before = generation_hash(workspace_dir)
    _note_rewrite_decline(PlanContext(ws=ws, store=store),
                          {"pattern": "p", "rewrite": "r"}, "engine_absent")
    _note_rewrite_decline(PlanContext(ws=ws, store=store),
                          {"pattern": "p2", "rewrite": "r2"}, "no_structural_match")
    assert generation_hash(workspace_dir) == before
