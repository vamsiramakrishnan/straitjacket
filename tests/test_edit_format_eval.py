"""The format × model replay: the receipt that decides whether an apply mode
for `ctx edit` is worth building. Model-free; these pin its arithmetic and
its refusals to over-read small cells."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import edit_format_by_model as E  # noqa: E402

from ctx.edit_outcomes import EDIT_OUTCOME_SCHEMA  # noqa: E402


def _row(model, fmt, outcome):
    return {"schema": EDIT_OUTCOME_SCHEMA, "ts": 0, "tool": "x", "outcome": outcome,
            "flavor": "t", "model": model, "format": fmt, "oldLen": 1, "newLen": 1}


def test_delta_is_anchored_minus_native_over_classified_rows():
    rows = (
        [_row("m", "search_replace", "applied")] * 30
        + [_row("m", "search_replace", "not_found")] * 10
        + [_row("m", "anchored", "applied")] * 36
        + [_row("m", "anchored", "not_unique")] * 4
        + [_row("m", "anchored", "unknown")] * 5   # leaves the denominator
    )
    rec = E.measure(rows)
    (m,) = rec["per_model"]
    assert m["native_classified"] == 40 and m["anchored_classified"] == 40
    assert m["delta_points"] == 15.0            # 90% − 75%
    assert m["verdict"] == "anchored_better"
    assert rec["anchored_better"] == 1 and rec["avg_delta_points"] == 15.0


def test_small_cells_are_insufficient_not_a_number():
    """Nine rows is not a rate. A delta over them would read like one."""
    rows = [_row("m", "search_replace", "applied")] * 9 + [_row("m", "anchored", "applied")] * 40
    (m,) = E.measure(rows)["per_model"]
    assert m["delta_points"] is None and m["verdict"] == "insufficient"


def test_native_better_is_reported_as_such():
    rows = ([_row("m", "search_replace", "applied")] * 30
            + [_row("m", "anchored", "applied")] * 20 + [_row("m", "anchored", "not_found")] * 10)
    (m,) = E.measure(rows)["per_model"]
    assert m["delta_points"] < 0 and m["verdict"] == "native_better"


def test_rows_without_a_model_are_counted_and_named_not_dropped():
    rows = [_row("m", "search_replace", "applied")] * 3 + [
        {**_row("", "search_replace", "applied"), "model": ""}] * 2
    rec = E.measure(rows)
    assert rec["rows"] == 5 and rec["unlabelled_model_rows"] == 2
    assert rec["models_reporting"] == ["m"]


def test_empty_ledger_infers_nothing(tmp_path):
    rec = E.run(tmp_path / "missing.jsonl", fixture=False)
    assert rec["rows"] == 0 and rec["measured_models"] == 0
    assert "nothing is inferred from an empty ledger" in E.render(rec)


def test_fixture_is_labelled_invented_and_the_script_runs():
    rec = E.run(None, fixture=True)
    assert rec["synthetic"] is True
    text = E.render(rec)
    assert "INVENTED" in text
    assert "external, not reproduced here" in text
    verdicts = {m["model"]: m["verdict"] for m in rec["per_model"]}
    assert verdicts["model-gamma"] == "insufficient"   # 12 rows a side
    assert verdicts["model-alpha"] == "anchored_better"

    proc = subprocess.run(
        [sys.executable, str(ROOT / "evals" / "edit_format_by_model.py"), "--fixture", "--json"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert json.loads(proc.stdout)["synthetic"] is True


def test_reads_a_real_ledger_file(tmp_path):
    ledger = tmp_path / "edit-outcomes.jsonl"
    ledger.write_text("".join(
        json.dumps(_row("m", f, o)) + "\n"
        for f, o in [("search_replace", "applied")] * 31 + [("anchored", "applied")] * 31
    ) + "not json\n")
    rec = E.run(ledger, fixture=False)
    assert rec["rows"] == 62 and rec["per_model"][0]["verdict"] == "tie"
