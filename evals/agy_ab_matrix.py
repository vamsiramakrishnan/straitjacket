#!/usr/bin/env python3
"""Aggregate `antigravity_sdk_eval.py` runs into one model x scenario matrix.

Each run dir holds a two-record `records.json` (the `naive` and `sj` arms of a
single A/B). This collapses many such dirs — repeats of the same cell included —
into one receipt: per (model, scenario) the mean billed tokens, the mean tool
output that entered context, and the billed dollar cost at the shipped list
prices, for both arms.

Usage:
  python evals/agy_ab_matrix.py evals/_runs/agy-* --out evals/antigravity-...md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from ctx.pricing import cost_usd  # noqa: E402

ARMS = ("naive", "sj")


def load(dirs: list[str]) -> dict[tuple[str, str], dict[str, list[dict]]]:
    cells: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for d in dirs:
        f = pathlib.Path(d) / "records.json"
        if not f.is_file():
            continue
        for rec in json.loads(f.read_text()):
            key = (rec["model"], rec.get("scenario", "?"))
            cells.setdefault(key, {a: [] for a in ARMS})[rec["arm"]].append(rec)
    return cells


def _mean(recs: list[dict], key: str) -> float:
    return statistics.fmean(r[key] for r in recs) if recs else 0.0


def _usd(recs: list[dict], model: str) -> float:
    if not recs:
        return 0.0
    return statistics.fmean(
        cost_usd({"input": r["billed_input_tokens"],
                  "output": r["billed_output_tokens"] + r.get("billed_thoughts_tokens", 0)},
                 model)
        for r in recs)


def render(cells) -> str:
    out = [
        "| model | scenario | n | arm | billed tokens | tool-output tokens in context "
        "| billed $ | correct |",
        "|---|---|--:|---|--:|--:|--:|:--:|",
    ]
    for (model, scenario), arms in sorted(cells.items()):
        n = max(len(arms[a]) for a in ARMS)
        for arm in ARMS:
            recs = arms[arm]
            if not recs:
                continue
            ok = sum(1 for r in recs if r["correct"])
            out.append(
                f"| `{model}` | {scenario} | {n} | {'naive' if arm == 'naive' else '**sj**'} "
                f"| {_mean(recs, 'billed_total_tokens'):,.0f} "
                f"| {_mean(recs, 'tool_output_tokens_into_context'):,.0f} "
                f"| ${_usd(recs, model):.4f} | {ok}/{len(recs)} |"
            )
        nv, sj = arms["naive"], arms["sj"]
        if nv and sj:
            bt = _mean(nv, "billed_total_tokens") / max(_mean(sj, "billed_total_tokens"), 1)
            to = _mean(nv, "tool_output_tokens_into_context") / max(
                _mean(sj, "tool_output_tokens_into_context"), 1)
            cu = _usd(nv, model) / max(_usd(sj, model), 1e-9)
            out.append(
                f"| | | | _ratio_ | **{bt:.1f}× less** | **{to:.0f}× less** "
                f"| **{cu:.1f}× less** | |"
            )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out")
    ns = ap.parse_args()
    table = render(load(ns.dirs))
    if ns.out:
        pathlib.Path(ns.out).write_text(table + "\n", "utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
