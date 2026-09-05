"""Edit format × model: does the anchored format beat the host's native one?

Aider's format ladder, hashline and EDIT-Bench all report the same thing:
the same model's edit success swings by tens of points on the SHAPE of the
edit alone -- needle-and-replacement, whole-file rewrite, unified patch,
anchored span -- and the ranking is per model, not universal. straitjacket
ships an anchored format (``ctx edit plan|preview|apply``) beside every
host's native ``Edit``/``Write``, and until now recorded neither which
format an edit used nor which model made it. Both are now on every
edit-outcome row. This reads that ledger and answers, per model:

    success(anchored) - success(native search/replace), with the counts.

It is a replay over recorded field rows, not a benchmark: nothing here
calls a model. Run it against a workspace that has had real sessions and
the table is field data; run it with ``--fixture`` and the table is a
synthetic ledger that only demonstrates the shape of the receipt. The
external bar the delta is read against (hashline's published +15 points
average, 14 of 16 models) is labelled as external and is NOT reproduced
here.

    python evals/edit_format_by_model.py                # ledger under CWD
    python evals/edit_format_by_model.py --ledger PATH  # any ledger file
    python evals/edit_format_by_model.py --fixture      # synthetic demo
    python evals/edit_format_by_model.py --json         # machine-readable
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ctx.edit_outcomes import (  # noqa: E402
    EDIT_OUTCOME_SCHEMA,
    FORMATS,
    load_rows,
    summarize_rows,
)
from ctx.sessiondir import session_reads_path  # noqa: E402

#: The native format the anchored one is compared against. Whole-file and
#: patch rows are reported but not used for the delta: the field's claim is
#: about search/replace needles specifically, and a delta over a blend would
#: not test that claim.
NATIVE = "search_replace"
ANCHORED = "anchored"

#: Below this many classified rows in BOTH cells a delta is reported as
#: ``insufficient`` rather than as a number. Thirty is not a significance
#: threshold; it is the point where one flaky session stops moving the rate
#: by double digits.
MIN_CELL = 30

#: What the delta is read against. External: measured elsewhere, on other
#: tasks, with other models. It says what a real gain looks like, not what
#: ours is.
EXTERNAL_BAR = {
    "source": "hashline (stencil.so), 16 models × 180 tasks",
    "avg_gain_points": 15.0,
    "models_improved": "14 of 16",
    "status": "external, not reproduced here",
}


def _fixture(seed: int = 7) -> list[dict]:
    """A synthetic ledger with a known shape, so the receipt renders without
    field data. Rates are invented; the point is that the table can be read."""
    rng = random.Random(seed)
    spec = {
        # model: (native success, anchored success, rows per format)
        "model-alpha": (0.62, 0.84, 60),
        "model-beta": (0.91, 0.93, 60),
        "model-gamma": (0.55, 0.58, 12),   # too few rows: insufficient
    }
    rows = []
    for model, (p_native, p_anchored, n) in spec.items():
        for fmt, p in ((NATIVE, p_native), (ANCHORED, p_anchored)):
            for _ in range(n):
                r = rng.random()
                if r < p:
                    outcome = "applied"
                elif r < p + (1 - p) * 0.7:
                    outcome = "not_found"
                elif r < p + (1 - p) * 0.9:
                    outcome = "not_unique"
                else:
                    outcome = "unknown"
                rows.append({
                    "schema": EDIT_OUTCOME_SCHEMA, "ts": 0,
                    "tool": "ctx edit apply" if fmt == ANCHORED else "Edit",
                    "outcome": outcome, "flavor": "fixture", "model": model,
                    "format": fmt, "oldLen": 40, "newLen": 44,
                })
    return rows


def measure(rows: list[dict]) -> dict:
    """Per model: the native and anchored cells and the delta between them."""
    summary = summarize_rows(rows)
    per_model = []
    for model, fmts in summary["by_model"].items():
        native = fmts.get(NATIVE)
        anchored = fmts.get(ANCHORED)
        n_native = native["classified"] if native else 0
        n_anchored = anchored["classified"] if anchored else 0
        if n_native >= MIN_CELL and n_anchored >= MIN_CELL:
            delta = round(100 * (anchored["success_rate"] - native["success_rate"]), 1)
            verdict = "anchored_better" if delta > 0 else (
                "native_better" if delta < 0 else "tie")
        else:
            delta = None
            verdict = "insufficient"
        per_model.append({
            "model": model,
            "formats": {
                fmt: {
                    "rows": cell["total"],
                    "classified": cell["classified"],
                    "success_pct": round(100 * cell["success_rate"], 1),
                    "unknown": cell["counts"]["unknown"],
                }
                for fmt, cell in fmts.items()
            },
            "native_classified": n_native,
            "anchored_classified": n_anchored,
            "delta_points": delta,
            "verdict": verdict,
        })
    measured = [m for m in per_model if m["delta_points"] is not None]
    return {
        "rows": summary["total"],
        "models_reporting": summary["models_reporting"],
        "unlabelled_model_rows": summary["unlabelled_model_rows"],
        "hosts_reporting": summary["hosts_reporting"],
        "min_cell": MIN_CELL,
        "per_model": per_model,
        "measured_models": len(measured),
        "anchored_better": sum(1 for m in measured if m["delta_points"] > 0),
        "avg_delta_points": (
            round(sum(m["delta_points"] for m in measured) / len(measured), 1)
            if measured else None
        ),
        "external_bar": EXTERNAL_BAR,
    }


def run(ledger: Path | None, fixture: bool) -> dict:
    if fixture:
        rows, source = _fixture(), "synthetic fixture (demonstrates the shape only)"
    else:
        path = ledger or session_reads_path(Path.cwd(), "edit-outcomes.jsonl")
        rows, source = load_rows(path), str(path)
    record = measure(rows)
    record["source"] = source
    record["synthetic"] = fixture
    return record


def render(rec: dict) -> str:
    out = ["[edit format × model · replay over recorded edit outcomes]", ""]
    out.append(f"source: {rec['source']}")
    if rec["synthetic"]:
        out.append("THESE NUMBERS ARE INVENTED. The fixture shows what the receipt "
                   "looks like; it says nothing about any model.")
    out.append(
        f"rows: {rec['rows']} · models named: {len(rec['models_reporting'])} · "
        f"rows with no model label: {rec['unlabelled_model_rows']} · "
        f"hosts: {', '.join(rec['hosts_reporting']) or '-'}"
    )
    out.append("")
    if not rec["rows"]:
        out.append("no edit-outcome rows. Run a session with the hook installed "
                   "(or `ctx orchestrate`) and re-run; nothing is inferred from "
                   "an empty ledger.")
        return "\n".join(out)
    head = f"{'model':24} {'format':14} {'rows':>5} {'classified':>10} {'success%':>9} {'unknown':>7}"
    out.append(head)
    for m in rec["per_model"]:
        for fmt in FORMATS:
            cell = m["formats"].get(fmt)
            if not cell:
                continue
            out.append(
                f"{m['model'][:24]:24} {fmt:14} {cell['rows']:>5} {cell['classified']:>10} "
                f"{cell['success_pct']:>9.1f} {cell['unknown']:>7}"
            )
    out.append("")
    out.append(f"delta = success(anchored) − success({NATIVE}), points; "
               f"a cell needs ≥ {rec['min_cell']} classified rows on both sides")
    out.append(f"{'model':24} {'native n':>8} {'anchored n':>10} {'delta':>7}  verdict")
    for m in rec["per_model"]:
        delta = "-" if m["delta_points"] is None else f"{m['delta_points']:+.1f}"
        out.append(
            f"{m['model'][:24]:24} {m['native_classified']:>8} {m['anchored_classified']:>10} "
            f"{delta:>7}  {m['verdict']}"
        )
    out.append("")
    if rec["measured_models"]:
        out.append(
            f"measured: anchored better on {rec['anchored_better']} of "
            f"{rec['measured_models']} models with enough rows · "
            f"average delta {rec['avg_delta_points']:+.1f} points"
        )
    else:
        out.append("measured: no model has enough rows on both formats yet")
    bar = rec["external_bar"]
    out.append(
        f"external bar ({bar['status']}): {bar['source']} — "
        f"+{bar['avg_gain_points']:.0f} points average, {bar['models_improved']} models improved"
    )
    return "\n".join(out)


def _args(argv: list[str]) -> tuple[Path | None, bool, bool]:
    ledger = None
    if "--ledger" in argv:
        ledger = Path(argv[argv.index("--ledger") + 1])
    return ledger, "--fixture" in argv, "--json" in argv


if __name__ == "__main__":
    ledger, fixture, as_json = _args(sys.argv[1:])
    record = run(ledger, fixture)
    print(json.dumps(record, indent=2, sort_keys=True) if as_json else render(record))
