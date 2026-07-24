#!/usr/bin/env python3
"""Live A/B for `ctx py`: mechanism-isolated arms on a held-out task.

Design (the Ponytail-ladder pattern — adopt on evidence):
- Both arms run a real agent (`claude -p`) against the same seeded fixture
  with `ctx` installed and one appended system-prompt line of routing
  doctrine. The ONLY difference between arms is whether that line permits
  `ctx py` — everything else (model, tools, task, fixture, turn cap) is
  identical, so the delta isolates the mechanism, not the harness.
- Held-out rule: the mechanical eval set (`evalset_collapse.py`) was tuned
  on per-module PASS RATES; this task asks for per-module P95 LATENCY over
  the same corpus shape — a variant the tuning never saw.
- Grading is mechanical: the slowest module and its p95 are recomputed
  independently here; an arm is correct iff its final answer names them.

Usage:
    python3 evals/ab_eval_live.py --out /tmp/ab-eval [--model haiku|sonnet]
                                  [--max-turns 20]

Requires: `claude` CLI on PATH with credentials, `ctx` installed
(`pip install -e .`). Each arm gets an isolated CLAUDE_CONFIG_DIR.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from evalset_collapse import build_fixture  # seeded corpus, same shape

MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": None}
TOOLS = "Bash Read Grep Glob"

TASK = (
    "The runs/ directory holds JSONL test logs; each record has fields "
    "test, module, ok, ms. Compute the p95 of ms per module using the "
    "nearest-rank method (sort ascending, take the value at index "
    "ceil(0.95*n)-1), then report every module's p95 sorted descending. "
    "Finish with exactly one line: 'SLOWEST: <module> p95=<value>'. "
    "Do not modify any files."
)

DOCTRINE = {
    "no-eval": (
        "ctx is installed. Route potentially large output through "
        "ctx run / ctx search / ctx get / ctx stats and cite handles. "
        "Do NOT use ctx py."
    ),
    # Scoped phrasing (debt c23a8ccdf5): terseness governs the SCRIPT's
    # output only. Runs 1-3 used an unscoped "print only what the
    # transcript needs" and 3/3 eval arms dropped the task's required
    # final line — the doctrine leak measured in the eval-collapse doc.
    "eval": (
        "ctx is installed. Route potentially large output through "
        "ctx run / ctx search / ctx get / ctx stats and cite handles. "
        "For multi-step data-dependent work (parse, aggregate, branch), "
        "prefer writing ONE short python script run as: ctx py '<script>'. "
        "Keep the script's printed output minimal — but your final answer "
        "must still satisfy the task's required output format in full."
    ),
}


def ground_truth(root: pathlib.Path) -> tuple[str, int]:
    import math

    per: dict[str, list[int]] = {}
    for p in sorted((root / "runs").glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            per.setdefault(rec["module"], []).append(rec["ms"])
    p95 = {
        m: sorted(v)[math.ceil(0.95 * len(v)) - 1] for m, v in per.items()
    }
    slowest = max(p95, key=lambda m: (p95[m], m))
    return slowest, p95[slowest]


def run_arm(arm: str, model: str, out: pathlib.Path, max_turns: int) -> dict:
    base = out / f"arm-{arm}"
    if base.exists():
        import shutil

        shutil.rmtree(base)
    build_fixture(base / "fixture")
    cfg = out / f"cc-{arm}"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg),
           "PIP_REQUIRE_VIRTUALENV": "1"}
    argv = ["claude", "-p", TASK, "--max-turns", str(max_turns),
            "--output-format", "json", "--allowedTools", TOOLS,
            "--append-system-prompt", DOCTRINE[arm]]
    if MODELS[model]:
        argv += ["--model", MODELS[model]]
    proc = subprocess.run(
        argv, cwd=base / "fixture", env=env,
        capture_output=True, text=True, timeout=1200,
    )
    (out / f"{arm}.raw.json").write_text(proc.stdout, encoding="utf-8")
    (out / f"{arm}.err").write_text(proc.stderr, encoding="utf-8")
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"arm": arm, "error": "unparseable output", "rc": proc.returncode}
    usage = doc.get("usage", {})
    return {
        "arm": arm,
        "num_turns": doc.get("num_turns"),
        "duration_s": round((doc.get("duration_ms") or 0) / 1000, 1),
        "cost_usd": round(doc.get("total_cost_usd") or 0.0, 4),
        "output_tokens": usage.get("output_tokens"),
        "input_tokens": usage.get("input_tokens"),
        "cache_read": usage.get("cache_read_input_tokens"),
        "cache_creation": usage.get("cache_creation_input_tokens"),
        "result_tail": (doc.get("result") or "")[-400:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="haiku", choices=list(MODELS))
    ap.add_argument("--max-turns", type=int, default=20)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    probe = args.out / "truth"
    build_fixture(probe / "fixture")
    slowest, value = ground_truth(probe / "fixture")
    print(f"ground truth: SLOWEST: {slowest} p95={value}", flush=True)

    rows = []
    for arm in ("no-eval", "eval"):
        print(f"running arm {arm} ({args.model}) ...", flush=True)
        r = run_arm(arm, args.model, args.out, args.max_turns)
        r["correct"] = (
            f"SLOWEST: {slowest}" in (r.get("result_tail") or "")
            and str(value) in (r.get("result_tail") or "")
        )
        rows.append(r)
        print(json.dumps(r, indent=2), flush=True)

    (args.out / "summary.json").write_text(
        json.dumps({"truth": {"slowest": slowest, "p95": value}, "arms": rows},
                   indent=2),
        encoding="utf-8",
    )
    print("AB_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
