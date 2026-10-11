#!/usr/bin/env python3
"""Extract a count-only receipt from the supplied Antigravity DeepSWE archive.

No commands, tool output, project identifiers or authentication data are copied.
Single-run observations and SDK-derived estimates remain explicitly labeled.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


def summarize(root: Path) -> dict:
    report = root / "docs/running-report.md"
    pairs = []
    for line in report.read_text().splitlines():
        match = re.match(r"\| `([^`]+)`", line)
        if not match:
            continue
        runs = re.findall(r"\[(run-[^]]+)\]", line)
        if len(runs) != 2:
            raise ValueError("pair row must name exactly two selected runs")
        arms = {}
        for arm, run in zip(("baseline", "straitjacket"), runs):
            metrics_paths = list((root / "results" / run).glob("*/metrics.json"))
            if len(metrics_paths) != 1:
                raise ValueError("selected run must have exactly one metrics record")
            metrics = json.loads(metrics_paths[0].read_text())
            if metrics["status"] != "graded":
                raise ValueError("selected attempt was not graded")
            cost = metrics.get("cost_estimate") or {}
            estimate_source = "metrics.json"
            if not cost and (root / "results" / run / "cost-estimate.json").is_file():
                cost = json.loads((root / "results" / run / "cost-estimate.json").read_text())
                estimate_source = "cost-estimate.json"
            # Legacy receipts predate the explicit gross-total field. Preserve
            # their declared separate-cache assumption; never subtract cached
            # tokens merely because a generic SDK description says subset.
            tokens = cost.get("token_counts") or {}
            gross = tokens.get("total_including_cache_derived")
            if gross is None and (cost.get("pricing") or {}).get("accounting") == "prompt_uncached_output_includes_thinking":
                uncached = tokens.get("input_uncached", tokens.get("input"))
                cached = tokens.get("input_cached", tokens.get("cached_input"))
                output = tokens.get("output_including_thinking")
                if all(type(v) is int and v >= 0 for v in (uncached, cached, output)):
                    gross = uncached + cached + output
            arms[arm] = {"run": run, "reward": metrics["reward"]["reward"],
                "agent_wall_seconds": metrics["agent_wall_seconds"],
                "gross_tokens_derived": gross,
                "estimated_model_usd": cost.get("estimated_cost_usd"),
                "estimate_source": estimate_source,
                "billed_model_usd": metrics.get("billed_cost_usd")}
        pairs.append({"task": match[1], **arms})
    counts = Counter(); sizes = Counter(); unknown = 0
    for path in sorted((root / "results").glob("run-*/*/straitjacket-calls.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line); args = row.get("args") or []
            if not args:
                continue
            counts[args[0]] += 1
            if args[0] == "run":
                value = row.get("captured_bytes")
                if type(value) is not int:
                    unknown += 1; continue
                sizes["known"] += 1
                sizes["empty"] += value == 0
                sizes["at_most_1kib"] += value <= 1024
                sizes["at_most_4kib"] += value <= 4096
    totals = {}
    for arm in ("baseline", "straitjacket"):
        selected = [p[arm] for p in pairs]
        totals[arm] = {"resolved": sum(r["reward"] == 1 for r in selected), "attempts": len(selected)}
        for key in ("agent_wall_seconds", "gross_tokens_derived", "estimated_model_usd", "billed_model_usd"):
            values = [r[key] for r in selected]
            totals[arm][key] = None if any(v is None for v in values) else sum(values)
    return {"schema": "ctx.agy-reference/v1", "source_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "pairs": pairs, "selected_totals": totals, "tool_calls": dict(counts),
        "command_sizes": {**dict(sizes), "unknown": unknown},
        "task_regressions": [p["task"] for p in pairs if p["straitjacket"]["reward"] < p["baseline"]["reward"]],
        "claim": "single_attempt_observations_not_causal_or_billing_evidence"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="extracted agy-sdk-evals/deepswe directory")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    text = json.dumps(summarize(args.reference), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True); args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
