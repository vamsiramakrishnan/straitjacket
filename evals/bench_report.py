#!/usr/bin/env python3
"""Aggregate a straitjacket-bench run (bench_run.py results.json) into the
per-scenario × arm table and by-flood-class / overall summaries.

Usage: python3 evals/bench_report.py <results.json> [--md]
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

ARMS = ("naive", "headroom", "sj")


def _cells(rows):
    """(scenario, arm) -> list of rows (over reps)."""
    d = defaultdict(list)
    for r in rows:
        d[(r["scenario"], r["arm"])].append(r)
    return d


def _rate(rows):
    return sum(1 for r in rows if r.get("success")) / len(rows) if rows else 0.0


def _med(rows, path):
    vals = []
    for r in rows:
        v = r
        for k in path:
            v = (v or {}).get(k) if isinstance(v, dict) else None
        if isinstance(v, (int, float)):
            vals.append(v)
    return statistics.median(vals) if vals else None


def _agg(rows):
    return {
        "n": len(rows),
        "success_rate": round(_rate(rows), 2),
        "med_turns": _med(rows, ["num_turns"]),
        "med_cost": round(_med(rows, ["cost_usd"]) or 0, 3),
        "med_wall": _med(rows, ["wall_s"]),
        "med_out_tok": _med(rows, ["tokens", "out"]),
        "med_cache_read": _med(rows, ["tokens", "cache_read"]),
    }


def build(rows):
    scenarios = sorted({r["scenario"] for r in rows})
    by_arm = {a: [r for r in rows if r["arm"] == a] for a in ARMS if any(r["arm"] == a for r in rows)}
    by_flood = {}
    for flood in ("none", "medium", "high"):
        by_flood[flood] = {
            a: _agg([r for r in rows if r["flood"] == flood and r["arm"] == a])
            for a in by_arm
        }
    cells = _cells(rows)
    per_scenario = {}
    for s in scenarios:
        meta = next(r for r in rows if r["scenario"] == s)
        per_scenario[s] = {
            "category": meta["category"], "flood": meta["flood"],
            "arms": {a: _agg(cells.get((s, a), [])) for a in by_arm},
        }
    overall = {a: _agg(by_arm[a]) for a in by_arm}
    # vocab adoption (sj)
    vocab = defaultdict(int)
    for r in rows:
        if r["arm"] == "sj":
            for v, n in (r.get("ctx_vocab") or {}).items():
                vocab[v] += n
    return {"overall": overall, "by_flood": by_flood,
            "per_scenario": per_scenario, "sj_vocab_total": dict(vocab)}


def _fmt(v):
    return "—" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def to_md(rep) -> str:
    arms = list(rep["overall"])
    out = ["## Overall (all scenarios)\n",
           "| arm | success | med turns | med cost | med wall | med out-tok | med cache-read |",
           "|---|---|---|---|---|---|---|"]
    for a in arms:
        g = rep["overall"][a]
        out.append(f"| {a} | {g['success_rate']:.0%} | {_fmt(g['med_turns'])} | "
                   f"${_fmt(g['med_cost'])} | {_fmt(g['med_wall'])}s | "
                   f"{_fmt(g['med_out_tok'])} | {_fmt(g['med_cache_read'])} |")
    out.append("\n## By flood class\n")
    out.append("| flood | " + " | ".join(f"{a} succ / turns / cost" for a in arms) + " |")
    out.append("|---|" + "|".join("---" for _ in arms) + "|")
    for flood in ("none", "medium", "high"):
        cells = []
        for a in arms:
            g = rep["by_flood"][flood].get(a) or {}
            if g.get("n"):
                cells.append(f"{g['success_rate']:.0%} / {_fmt(g['med_turns'])} / ${_fmt(g['med_cost'])}")
            else:
                cells.append("—")
        out.append(f"| {flood} | " + " | ".join(cells) + " |")
    out.append("\n## Per scenario (success · turns · cost)\n")
    out.append("| scenario | cat | flood | " + " | ".join(arms) + " |")
    out.append("|---|---|---|" + "|".join("---" for _ in arms) + "|")
    for s, d in rep["per_scenario"].items():
        cells = []
        for a in arms:
            g = d["arms"].get(a) or {}
            if g.get("n"):
                ok = "✅" if g["success_rate"] >= 0.5 else "❌"
                cells.append(f"{ok} {_fmt(g['med_turns'])}t ${_fmt(g['med_cost'])}")
            else:
                cells.append("—")
        out.append(f"| {s} | {d['category']} | {d['flood']} | " + " | ".join(cells) + " |")
    out.append(f"\nsj vocab adoption (total ctx verb calls): {rep['sj_vocab_total']}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", type=Path)
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args()
    data = json.loads(args.results.read_text())
    rep = build(data["rows"])
    if args.md:
        print(to_md(rep))
    else:
        print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
