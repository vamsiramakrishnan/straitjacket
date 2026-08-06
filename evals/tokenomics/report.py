#!/usr/bin/env python3
"""Render the triage-mode referee. A pure function of results/*.json.

Every number below is recomputed from the per-task records at render time.
Nothing is carried in prose, so a table cannot drift from its data -- the
failure mode that made the suite this mirrors unreportable.

Usage:
    python evals/tokenomics/report.py --results evals/tokenomics/results
    python evals/tokenomics/report.py --results ... --prices prices.json
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. At n=30 this is wide, which is the point."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial p for paired discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def usd(tokens_in: int, tokens_out: int, model: str, prices: dict) -> float:
    p = prices.get(model)
    if not p:
        return 0.0
    return tokens_in / 1e6 * p["input"] + tokens_out / 1e6 * p["output"]


def summarise(payload: dict, prices: dict | None = None) -> dict:
    prices = prices or payload.get("prices_usd_per_mtok", {})
    rows = payload["results"]

    counts = {"passed": 0, "failed": 0, "infra_error": 0, "errored": 0}
    solver_in = solver_out = triage_in = triage_out = 0
    solver_usd = triage_usd = 0.0
    channel_chars = truncated = hops = 0
    per_task = {}

    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        per_task[r["task_id"]] = r["status"] == "passed"
        for c in r.get("calls", []):
            solver_in += c["input_tokens"]
            solver_out += c["output_tokens"]
            solver_usd += usd(c["input_tokens"], c["output_tokens"], c["model"], prices)
            if str(c.get("finish_reason", "")).endswith("MAX_TOKENS"):
                truncated += 1
        for c in r.get("triage_calls", []):
            triage_in += c["input_tokens"]
            triage_out += c["output_tokens"]
            triage_usd += usd(c["input_tokens"], c["output_tokens"], c["model"], prices)
        channel_chars += r.get("channel_chars", 0)
        hops += r.get("hops", 0)

    n = payload["n"]
    scored = n - counts["errored"] - counts["infra_error"]
    total_usd = solver_usd + triage_usd
    lo, hi = wilson(counts["passed"], n)

    return {
        "arm": payload["arm"],
        "family": payload["family"],
        "triage": payload["triage"],
        "ladder": " -> ".join(
            f"{l['model'].replace('gemini-', '')}" + (f"({l['thinking'].lower()})" if l["thinking"] else "")
            for l in payload["ladder"]
        ),
        "n": n,
        "scored": scored,
        **counts,
        "pass_rate": counts["passed"] / n if n else 0.0,
        "ci_low": lo,
        "ci_high": hi,
        "solver_tokens_in": solver_in,
        "solver_tokens_out": solver_out,
        "triage_tokens_in": triage_in,
        "triage_tokens_out": triage_out,
        "triage_usd": triage_usd,
        "total_usd": total_usd,
        "usd_per_solved": total_usd / counts["passed"] if counts["passed"] else float("nan"),
        "repair_channel_chars": channel_chars,
        "retrieval_hops": hops,
        "truncated_calls": truncated,
        "wall_seconds": payload.get("wall_seconds", 0),
        "per_task": per_task,
        "provenance": payload.get("provenance"),
        "simulated": payload.get("simulated"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=pathlib.Path, default=pathlib.Path(__file__).parent / "results")
    ap.add_argument("--prices", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    args = ap.parse_args()

    prices = json.loads(args.prices.read_text()) if args.prices else None
    payloads = [json.loads(p.read_text()) for p in sorted(args.results.glob("*.json"))]
    if not payloads:
        raise SystemExit(f"no results in {args.results}")

    # Refuse to render anything that did not come from live calls.
    bad = [p["arm"] for p in payloads if p.get("simulated") or p.get("provenance") != "live"]
    if bad:
        raise SystemExit(f"refusing to report simulated arms: {bad}")

    summaries = [summarise(p, prices) for p in payloads]
    price_note = payloads[0].get("price_provenance", "")

    L = []
    L.append("# Triage-channel referee — BigCodeBench-Hard\n")
    ids = payloads[0]["task_ids"]
    same = all(p["task_ids"] == ids for p in payloads)
    L.append(f"- Tasks: **{len(ids)}**, identical across arms: **{same}**")
    L.append(f"- Provenance: **live API calls only** (simulated arms are refused by this generator)")
    L.append(f"- Sandbox: `{payloads[0].get('sandbox_python','?')}`")
    L.append(f"- Price table: {price_note}\n")

    L.append("## Per-arm results\n")
    L.append(
        "| Arm | Ladder | Triage | Pass | Rate (95% CI) | Solver tok (in/out) | "
        "Triage tok | Triage $ | Total $ | $/solved | Repair-channel chars | ctx get hops |"
    )
    L.append("|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|")
    for s in sorted(summaries, key=lambda x: (x["family"], x["triage"])):
        L.append(
            f"| `{s['arm']}` | {s['ladder']} | {s['triage']} | {s['passed']}/{s['n']} | "
            f"{s['pass_rate']:.1%} ({s['ci_low']:.0%}–{s['ci_high']:.0%}) | "
            f"{s['solver_tokens_in']:,}/{s['solver_tokens_out']:,} | "
            f"{s['triage_tokens_in'] + s['triage_tokens_out']:,} | "
            f"${s['triage_usd']:.4f} | ${s['total_usd']:.4f} | "
            f"${s['usd_per_solved']:.4f} | {s['repair_channel_chars']:,} | {s['retrieval_hops']} |"
        )

    L.append("\n## Run health (excluded and degraded tasks are counted, never dropped silently)\n")
    L.append("| Arm | passed | failed | infra_error | errored (API) | scored | truncated calls | wall s |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for s in sorted(summaries, key=lambda x: (x["family"], x["triage"])):
        L.append(
            f"| `{s['arm']}` | {s['passed']} | {s['failed']} | {s['infra_error']} | "
            f"{s['errored']} | {s['scored']} | {s['truncated_calls']} | {s['wall_seconds']:.0f} |"
        )

    L.append("\n## Paired comparison within each family (McNemar, exact)\n")
    L.append("Same tasks, same ladder, same prompts — only the triage channel differs.\n")
    L.append("| Family | A | B | A only | B only | both | neither | p |")
    L.append("|---|---|---|---:|---:|---:|---:|---:|")
    by_family: dict[str, list] = {}
    for s in summaries:
        by_family.setdefault(s["family"], []).append(s)
    for fam, group in sorted(by_family.items()):
        group = sorted(group, key=lambda x: x["triage"])
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                shared = set(a["per_task"]) & set(b["per_task"])
                a_only = sum(1 for t in shared if a["per_task"][t] and not b["per_task"][t])
                b_only = sum(1 for t in shared if b["per_task"][t] and not a["per_task"][t])
                both = sum(1 for t in shared if a["per_task"][t] and b["per_task"][t])
                neither = sum(1 for t in shared if not a["per_task"][t] and not b["per_task"][t])
                p = mcnemar_exact(a_only, b_only)
                L.append(
                    f"| {fam} | `{a['triage']}` | `{b['triage']}` | {a_only} | {b_only} | "
                    f"{both} | {neither} | {p:.3f} |"
                )

    L.append("\n## What this measures\n")
    L.append(
        "The triage channel is the only manipulated variable inside a family. `raw` forwards the "
        "unittest stderr verbatim, `llm` pays a model to compress it, `sj` forwards the digest "
        "emitted by the real `ctx run` CLI, and `sj_hop` forwards that digest plus the spans it "
        "cites, resolved locally with `ctx get`. Pass-rate differences inside a family are "
        "attributable to that channel; differences across families are not (the ladder changes too)."
    )
    L.append(
        "\nThe triage-cost column is the mechanical claim and it is exact: `raw`, `sj` and `sj_hop` "
        "make no triage API call, so their triage cost is $0.0000 by construction — `ctx get` reads "
        "the local store. `llm` pays per repair loop. Pass rate is the gate — a cheaper channel is "
        "only interesting if accuracy holds, and at this N the confidence intervals are wide enough "
        "that small differences are not resolved."
    )
    L.append(
        "\n**Caveat on `Repair-channel chars`:** it sums the final channel of FAILED tasks only, so "
        "an arm that passes more tasks scores lower for free. It mixes channel size with failure "
        "count and is NOT a clean bytes-per-repair comparison across arms; measuring that properly "
        "needs a per-repair-loop metric the runner does not yet record."
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
