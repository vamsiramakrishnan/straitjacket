#!/usr/bin/env python3
"""Sustained bug-bash loop: run arms back to back, fix between rounds.

Each round runs one arm against whatever HEAD is at that moment, so fixes
landed between rounds are exactly what the next arm is measured against.
Findings stream into findings.jsonl as the arm proves them, which means the
operator does not have to wait for a round to finish before starting work --
that append-only ledger is what makes "don't wait for them to complete"
possible at all.

Every round appends one row to ledger.jsonl: cost, turns, wall, the flood
metrics, and confirmed/claimed. That ledger is the efficacy record -- whether
fixing bugs makes later rounds cheaper, quieter, or merely different, and
whether a cheaper model still finds real defects.

Usage:
    python3 evals/devex/loop.py --out /tmp/loop --rounds 10 --model sonnet
    python3 evals/devex/loop.py --out /tmp/loop --rounds 1 --model haiku
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parent.parent

import bugbash as B  # noqa: E402


def head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--arm", default="harnessed",
                    choices=["harnessed", "naive"])
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=2400)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    ledger = a.out / "ledger.jsonl"

    for rnd in range(1, a.rounds + 1):
        base = head()
        rd = a.out / f"round-{rnd:02d}"
        rd.mkdir(parents=True, exist_ok=True)
        print(f"\n=== round {rnd}/{a.rounds}  base={base[:12]}  "
              f"model={a.model} ===", flush=True)
        t0 = time.time()
        try:
            B.run_arm(a.arm, rd, base, a.max_turns, a.model, a.timeout)
        except Exception as e:  # a dead round must not kill the loop
            print(f"  round {rnd} run failed: {type(e).__name__}: {e}", flush=True)
        m = {}
        try:
            m = B.instrument(rd / f"arm-{a.arm}" / "stream.jsonl")
        except Exception as e:
            print(f"  instrument failed: {e}", flush=True)
        v = {}
        try:
            v = B.verify(a.arm, rd, base)
        except Exception as e:
            print(f"  verify failed: {e}", flush=True)
        row = {
            "round": rnd, "base": base, "model": a.model, "arm": a.arm,
            "wall_s": round(time.time() - t0, 1),
            "cost_usd": m.get("cost_usd"), "turns": m.get("turns"),
            "tool_calls": m.get("tool_calls_total"),
            "result_bytes": m.get("tool_result_bytes"),
            "floods": m.get("tool_results_over_10kb"),
            "bytes_per_result": m.get("bytes_per_result"),
            "cache_hit_ratio": m.get("cache_hit_ratio"),
            "claimed": v.get("claimed"), "confirmed": v.get("confirmed"),
            "precision": v.get("precision"),
            "confirmed_ids": [f.get("id") for f in v.get("findings", [])
                              if f.get("verdict") == "CONFIRMED"],
        }
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"  cost=${row['cost_usd']} turns={row['turns']} "
              f"bytes={row['result_bytes']} floods={row['floods']} "
              f"CONFIRMED={row['confirmed']}/{row['claimed']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
