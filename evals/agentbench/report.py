#!/usr/bin/env python3
"""Render the agent-harness referee. A pure function of results/*.json.

The headline is NOT resolve rate. Per `evals/BENCHMARK.md`, the load-bearing
gate is evidence preservation -- solved-under-sj / solved-native -- and no
efficiency number is reportable unless that stays about 1.0. Only then do turns,
tokens, cache and wall-clock mean anything, because a harness that finishes
cheaper by failing more is not cheaper.

Usage:
    python evals/agentbench/report.py --results evals/agentbench/results
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def summarise(records: list[dict], arm: str) -> dict:
    rows = [r for r in records if r["arm"] == arm]
    # Median across repeats per task, then aggregate -- BENCHMARK.md's
    # determinism discipline, since temperature and seed are not controllable.
    by_task: dict[str, list[dict]] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)

    resolved = {}
    for tid, runs in by_task.items():
        wins = sum(1 for r in runs if r.get("resolved"))
        resolved[tid] = wins * 2 > len(runs)  # majority across repeats

    return {
        "arm": arm,
        "tasks": len(by_task),
        "runs": len(rows),
        "resolved": sum(resolved.values()),
        "per_task": resolved,
        "turns": _median([r.get("turns") for r in rows]),
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in rows), 4),
        "cache_hit_pct": _median([r.get("cache_hit_pct") for r in rows]),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in rows),
        "uncached_in": sum(r.get("uncached_in") or 0 for r in rows),
        # Total input is what the transcript actually costs. `uncached_in` alone
        # is near-zero once prompt caching engages and reads as "no context used".
        "total_in": sum((r.get("cache_read") or 0) + (r.get("cache_write") or 0)
                        + (r.get("uncached_in") or 0) for r in rows),
        "wall_s": _median([r.get("wall_s") for r in rows]),
        "timed_out": sum(1 for r in rows if r.get("timed_out")),
        "session_errors": sum(1 for r in rows if r.get("session_error")),
        "tampered": sum(1 for r in rows if r.get("tests_tampered")),
        # Open-ended missions have a YIELD, not just a pass/fail. `resolved`
        # for the bugbash means "at least one defect reproduced", so two arms
        # finding 8 and 5 both score 1/1 and the report calls it a tie. Carry
        # the count so the headline cannot hide the difference.
        "reproduced": sum(r.get("reproduced") or 0 for r in rows),
        "has_yield": any("reproduced" in r for r in rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=pathlib.Path,
                    default=pathlib.Path(__file__).parent / "results")
    ap.add_argument("--out", type=pathlib.Path)
    args = ap.parse_args()

    payloads = [json.loads(p.read_text()) for p in sorted(args.results.glob("*.json"))]
    if not payloads:
        raise SystemExit(f"no results in {args.results}")
    bad = [p.get("adapter") for p in payloads if p.get("simulated") or p.get("provenance") != "live"]
    if bad:
        raise SystemExit(f"refusing to report simulated runs: {bad}")

    L = ["# Agent-harness referee\n"]
    for payload in payloads:
        records = payload["results"]
        arms = payload["arms"]
        L.append(f"## adapter: `{payload['adapter']}`\n")
        L.append(f"- Tasks: **{len(payload['task_ids'])}** · repeats: **{payload['repeats']}** "
                 f"· max turns: {payload['max_turns']} · model: {payload.get('model') or 'host default (not recorded)'}")
        L.append("- Arms: plain `claude` vs the full `ctx wrap claude --proxy` intervention; effective prompt/tools may differ")
        L.append("- Provenance: **live agent sessions** (simulated runs are refused)\n")

        summaries = {a: summarise(records, a) for a in arms}

        L.append("| Arm | Resolved | Median turns | Median cache hit | Total input tok | Output tok | Cost $ | Median wall s | Timeouts |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for a in arms:
            s = summaries[a]
            L.append(
                f"| `{a}` | {s['resolved']}/{s['tasks']} | {s['turns']} | "
                f"{s['cache_hit_pct']}% | {s['total_in']:,} | {s['output_tokens']:,} | "
                f"${s['cost_usd']:.4f} | {s['wall_s']} | {s['timed_out']} |"
            )

        if any(s["has_yield"] for s in summaries.values()):
            L.append("\n### Yield (open-ended mission: count, not pass/fail)\n")
            L.append("`resolved` only asks whether an arm produced ANY result. For a mission "
                     "with an open-ended count, that collapses very different outcomes into "
                     "the same cell.\n")
            L.append("| Arm | Failing test nodes reproduced | Cost per reproduction |")
            L.append("|---|---:|---:|")
            for a in arms:
                sm = summaries[a]
                per = (sm["cost_usd"] / sm["reproduced"]) if sm["reproduced"] else float("nan")
                L.append(f"| `{a}` | {sm['reproduced']} | ${per:.2f} |")

        if "naive" in summaries:
            base = summaries["naive"]["resolved"]
            L.append("\n### Evidence-preservation gate\n")
            L.append("`solved_arm / solved_naive` must hold at ~1.0. Nothing below is reportable otherwise.\n")
            L.append("| Arm | Resolved | Ratio vs naive | Gate |")
            L.append("|---|---:|---:|---|")
            for a in arms:
                s = summaries[a]
                ratio = (s["resolved"] / base) if base else float("nan")
                gate = "PASS" if base and ratio >= 0.95 else ("—" if a == "naive" else "FAIL")
                if a == "naive":
                    gate = "baseline"
                L.append(f"| `{a}` | {s['resolved']}/{s['tasks']} | {ratio:.2f} | {gate} |")

            L.append("\n### Paired outcome (McNemar, exact)\n")
            L.append("| A | B | A only | B only | both | neither | p |")
            L.append("|---|---|---:|---:|---:|---:|---:|")
            for i, a in enumerate(arms):
                for b in arms[i + 1:]:
                    pa, pb = summaries[a]["per_task"], summaries[b]["per_task"]
                    shared = set(pa) & set(pb)
                    a_only = sum(1 for t in shared if pa[t] and not pb[t])
                    b_only = sum(1 for t in shared if pb[t] and not pa[t])
                    both = sum(1 for t in shared if pa[t] and pb[t])
                    neither = sum(1 for t in shared if not pa[t] and not pb[t])
                    L.append(f"| `{a}` | `{b}` | {a_only} | {b_only} | {both} | {neither} | "
                             f"{mcnemar_exact(a_only, b_only):.3f} |")

        tampered = sum(s["tampered"] for s in summaries.values())
        errs = sum(s["session_errors"] for s in summaries.values())
        if tampered or errs:
            L.append(f"\n> Run health: {tampered} run(s) modified instance tests (scored unresolved), "
                     f"{errs} session(s) produced no result JSON.")

    L.append(
        "\n## Reading this\n\nResolve rate is a gate, not a headline: the claim this harness can "
        "support is *matched-or-better success, then fewer turns, tokens, and seconds*. A wrapper "
        "that resolves fewer tasks has not saved anything, however good its token column looks."
    )

    text = "\n".join(L) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
