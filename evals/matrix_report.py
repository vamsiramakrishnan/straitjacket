#!/usr/bin/env python3
"""Grade a matrix run's measurement axes: cost (w), tokens (x), turns (y),
time (z) — plus the cache economics that connect them.

Cache metrics answer one question honestly: did a change keep token counts
low while quietly raising cost through prefix churn? (The Headroom lesson:
cache_creation is billed at a premium, so a low-token, high-churn run can
cost MORE than a high-token, cache-stable one.)

Per cell (result JSON, all models summed — forks count):
  cost, turns, duration, output tokens, prompt tokens by class
  cache_hit  = cache_read / (input + cache_read + cache_creation)
  churn      = cache_creation / cache_read
  creation_per_request — prefix-invalidation pressure normalized by length

Per proxied cell (wire.jsonl ground truth, per-exchange):
  cold_prefix_tok: creation on the first content request that read nothing
  from cache — a one-time cost, amortized across later sessions/arms.
  invalidations: requests where cache_read REGRESSED vs the running max —
  the only true "cache break" signal. Incremental creation with monotone
  read is normal suffix growth, not churn.
  timing split: connect / ttfb (queue+prefill) / generation (total - ttfb)

Usage: python3 evals/matrix_report.py --out /path/to/matrix-dir
"""
from __future__ import annotations

import argparse
import json
import pathlib

# Single source of truth for prices lives in the package (ctx.scorecard);
# this report reuses it so the two can never drift.
import sys as _sys

_sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from ctx.scorecard import PRICES as _SC_PRICES  # noqa: E402
from ctx.scorecard import _price_key  # noqa: E402

PRICES = {
    k: {"in": v["in"], "out": v["out"], "write": v["write"], "read": v["read"]}
    for k, v in _SC_PRICES.items()
}


def read_result(path: pathlib.Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    usage = {"input": 0, "output": 0, "read": 0, "creation": 0}
    decomp = {"in": 0.0, "out": 0.0, "write": 0.0, "read": 0.0}
    for model_id, mu in (doc.get("modelUsage") or {}).items():
        usage["input"] += mu.get("inputTokens", 0)
        usage["output"] += mu.get("outputTokens", 0)
        usage["read"] += mu.get("cacheReadInputTokens", 0)
        usage["creation"] += mu.get("cacheCreationInputTokens", 0)
        p = PRICES[_price_key(model_id)]
        decomp["in"] += mu.get("inputTokens", 0) * p["in"] / 1e6
        decomp["out"] += mu.get("outputTokens", 0) * p["out"] / 1e6
        decomp["write"] += mu.get("cacheCreationInputTokens", 0) * p["write"] / 1e6
        decomp["read"] += mu.get("cacheReadInputTokens", 0) * p["read"] / 1e6
    prompt = usage["input"] + usage["read"] + usage["creation"]
    return {
        "ok": doc.get("subtype") == "success",
        "subtype": doc.get("subtype"),
        "cost": doc.get("total_cost_usd"),
        "turns": doc.get("num_turns"),
        "duration_s": (doc.get("duration_ms") or 0) / 1000,
        "output_tok": usage["output"],
        "prompt_tok": prompt,
        "uncached_in": usage["input"],
        "cache_read": usage["read"],
        "cache_creation": usage["creation"],
        "cache_hit": usage["read"] / prompt if prompt else 0.0,
        "churn": usage["creation"] / usage["read"] if usage["read"] else float("inf"),
        "cost_decomp": decomp,
        "result_tail": str(doc.get("result") or "")[-400:],
    }


def read_wire(fixture: pathlib.Path) -> dict | None:
    wire = fixture / ".ctx-session-reads" / "proxy" / "wire.jsonl"
    if not wire.is_file():
        return None
    msgs, invalidations, cold_prefix = 0, 0, 0
    max_read = 0
    ongoing_creation = 0
    t = {"connect": 0.0, "ttfb": 0.0, "gen": 0.0}
    reused = 0
    for line in wire.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not rec.get("path", "").endswith("/messages"):
            continue
        usage = rec.get("usage") or {}
        if not usage:
            continue
        msgs += 1
        creation = usage.get("cache_creation_input_tokens", 0)
        read = usage.get("cache_read_input_tokens", 0)
        # Side-channel requests (quota/title) read 0 and create 0; the cold
        # prefix write is the first request that creates a lot reading nothing.
        if read == 0 and creation > 4096 and cold_prefix == 0:
            cold_prefix = creation
        elif read and read < max_read and rec.get("messages", 0) > 1:
            # A fresh single-message thread (title gen, fork start) is not a
            # regression of the main thread's prefix; a multi-message request
            # reading less than the running max is.
            invalidations += 1
            ongoing_creation += creation
        else:
            ongoing_creation += creation
        max_read = max(max_read, read)
        ms = rec.get("ms") or {}
        t["connect"] += ms.get("connect", 0.0)
        t["ttfb"] += ms.get("ttfb", 0.0)
        t["gen"] += max(0.0, ms.get("total", 0.0) - ms.get("ttfb", 0.0))
        reused += 1 if rec.get("reused_conn") else 0
    if not msgs:
        return None
    return {
        "requests": msgs,
        "cold_prefix_tok": cold_prefix,
        "invalidations": invalidations,
        "ongoing_creation": ongoing_creation,
        "reused_pct": 100 * reused / msgs,
        "connect_s": t["connect"] / 1000,
        "ttfb_s": t["ttfb"] / 1000,
        "gen_s": t["gen"] / 1000,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args()
    cells = {}
    for rf in sorted(args.out.glob("S*_*.json")):
        cell = rf.stem  # S1_sonnet_naive
        res = read_result(rf)
        if res is None:
            continue
        scenario = cell.split("_")[0]
        fixture = args.out / cell
        if scenario in ("S3", "S4"):
            fixture = fixture / "r"
        res["wire"] = read_wire(fixture)
        cells[cell] = res
    print(json.dumps(cells, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
