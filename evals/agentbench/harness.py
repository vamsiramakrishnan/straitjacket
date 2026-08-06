#!/usr/bin/env python3
"""Agent-harness referee: the wrapper is the only variable.

The tokenomics eval (`evals/tokenomics/`) drives a fixed model ladder -- a
script that calls an API, runs a subprocess, calls an API again. That measures a
digest formatter, because nothing in the loop can decide to run a command or
follow an address. This harness drives a REAL agent instead, and the arms differ
in exactly one thing:

    naive :  claude -p "<task>" --max-turns N --allowedTools "..."
    sj    :  ctx wrap claude --proxy -- -p "<task>" --max-turns N ...

Same model, same fixture, same prompt, same tools, same turn cap. Fixtures carry
`ctx.toml` and git for BOTH arms so the tree shape is identical. The agent runs
the noisy suite itself, floods itself, and retrieves itself -- which is the mode
straitjacket is built for and the fixed ladder structurally cannot reach.

Arm construction follows `evals/spec3_runner.py` (the frozen referee) so numbers
from the two harnesses stay comparable.

Benchmarks plug in as adapters: an adapter materializes a fixture and grades the
result. Swapping SWE-bench for Terminal-Bench swaps the adapter, not the harness.

Usage:
    python evals/agentbench/harness.py --adapter canary --arms naive sj --n 4
    python evals/agentbench/harness.py --adapter swebench --n 60 --repeats 3
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import time

HERE = pathlib.Path(__file__).resolve().parent

# Kept byte-identical to spec3_runner's referee constants where they overlap.
TOOLS = "Bash Read Grep Glob Edit Write"
MAX_TURNS = 40
SESSION_TIMEOUT = 2400

ARMS = ("naive", "sj", "headroom")


def arm_argv(arm: str, prompt: str, model: str | None, max_turns: int) -> list[str]:
    """Build the agent command line. Only the wrapper prefix differs."""
    base = [
        "claude", "-p", prompt,
        "--max-turns", str(max_turns),
        "--output-format", "json",
        "--allowedTools", TOOLS,
    ]
    if model:
        base += ["--model", model]
    if arm == "naive":
        return base
    if arm == "sj":
        return ["ctx", "wrap", "claude", "--proxy", "--"] + base[1:]
    if arm == "headroom":
        return ["headroom", "wrap", "claude", "--"] + base[1:]
    raise ValueError(f"unknown arm: {arm}")


def parse_result_json(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def session_metrics(doc: dict, wall: float) -> dict:
    u = doc.get("usage", {}) or {}
    reads = u.get("cache_read_input_tokens") or 0
    writes = u.get("cache_creation_input_tokens") or 0
    uncached = u.get("input_tokens") or 0
    denom = reads + writes + uncached
    return {
        "turns": doc.get("num_turns"),
        "cost_usd": round(doc.get("total_cost_usd") or 0.0, 4),
        "api_duration_s": round((doc.get("duration_ms") or 0) / 1000, 1),
        "wall_s": round(wall, 1),
        "cache_hit_pct": round(100 * reads / denom, 1) if denom else None,
        "cache_read": reads,
        "cache_write": writes,
        "uncached_in": uncached,
        "output_tokens": u.get("output_tokens"),
        "session_error": doc == {},
    }


def run_one(adapter, task: dict, arm: str, model: str | None, out: pathlib.Path,
            max_turns: int, repeat: int) -> dict:
    """One (task, arm, repeat): materialize, run the agent, grade."""
    tag = f"{task['id']}_{arm}_r{repeat}".replace("/", "_")
    workdir = out / "work" / tag
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    prompt = adapter.prepare(task, workdir)

    # Isolated agent config per run, so one arm cannot warm another's state.
    cfg = out / "cfg" / tag
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg), "PIP_REQUIRE_VIRTUALENV": "1"}

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            arm_argv(arm, prompt, model, max_turns),
            cwd=workdir, env=env, capture_output=True, text=True,
            timeout=SESSION_TIMEOUT,
        )
        stdout, stderr, timed_out = proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timed_out = True
    wall = time.monotonic() - t0

    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "logs" / f"{tag}.stdout").write_text(stdout, encoding="utf-8")
    (out / "logs" / f"{tag}.stderr").write_text(stderr, encoding="utf-8")

    doc = parse_result_json(stdout)
    rec = {
        "task_id": task["id"],
        "arm": arm,
        "repeat": repeat,
        "timed_out": timed_out,
        "provenance": "live",
        **session_metrics(doc, wall),
    }
    # Grading is the adapter's job and never trusts the agent's own claims.
    rec.update(adapter.grade(task, workdir))
    return rec


def load_adapter(name: str):
    return importlib.import_module(f"adapters.{name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="canary | swebench")
    ap.add_argument("--arms", nargs="+", default=["naive", "sj"], choices=ARMS)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--model", default=None, help="passed to --model; default = host default")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "results")
    ap.add_argument("--adapter-arg", action="append", default=[],
                    help="key=value passed through to the adapter's load()")
    args = ap.parse_args()

    import sys
    sys.path.insert(0, str(HERE))
    adapter = load_adapter(args.adapter)

    kw = {}
    for item in args.adapter_arg:
        k, _, v = item.partition("=")
        kw[k] = v
    tasks = adapter.load(args.n, **kw)
    if not tasks:
        raise SystemExit("adapter returned no tasks")

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"adapter={args.adapter} tasks={len(tasks)} arms={args.arms} repeats={args.repeats}",
          flush=True)

    records = []
    for repeat in range(1, args.repeats + 1):
        for task in tasks:
            for arm in args.arms:
                rec = run_one(adapter, task, arm, args.model, args.out,
                              args.max_turns, repeat)
                records.append(rec)
                print(
                    f"  [{arm}] r{repeat} {task['id']:44s} "
                    f"resolved={str(rec.get('resolved')):5s} turns={rec.get('turns')} "
                    f"cache={rec.get('cache_hit_pct')}% {rec.get('wall_s')}s",
                    flush=True,
                )
                payload = {
                    "schema": "agentbench.run/v1",
                    "adapter": args.adapter,
                    "arms": args.arms,
                    "model": args.model,
                    "max_turns": args.max_turns,
                    "repeats": args.repeats,
                    "task_ids": [t["id"] for t in tasks],
                    "provenance": "live",
                    "simulated": False,
                    "results": records,
                }
                (args.out / f"{args.adapter}.json").write_text(
                    json.dumps(payload, indent=1), encoding="utf-8")

    print(f"-> {args.out / f'{args.adapter}.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
