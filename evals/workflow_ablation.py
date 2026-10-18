#!/usr/bin/env python3
"""Pinned, repeated tool-workflow ablations using the existing DeepSWE referee.

freeze/report never call a model. run is explicitly live. Missing billing or
model-request observations remain null; tool calls are never renamed turns.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "rich": ["--result-format", "json", "--no-navigation", "--no-composition"],
    "compact": ["--result-format", "compact", "--no-navigation", "--no-composition"],
    "navigation": ["--result-format", "compact", "--no-composition"],
    "composed": ["--result-format", "compact"],
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_adapter(name):
    sys.path.insert(0, str(ROOT / "evals/agentbench"))
    return importlib.import_module("adapters." + name)


def freeze(wheel, *, source_sha, sdk_version, model, tasks, repeats=3, timeout=2400,
           rates=None, input_semantics="unknown"):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha) or not sdk_version or not model:
        raise ValueError("pin source commit, provider SDK version and model")
    if type(repeats) is not int or repeats < 3 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("use at least three matched repeats and a positive timeout")
    if not tasks or len({t["id"] for t in tasks}) != len(tasks):
        raise ValueError("task selection must be nonempty and unique")
    if any(not t.get("corpus_commit") or not t.get("base_commit") for t in tasks):
        raise ValueError("task corpus and repository commits must be pinned")
    if rates is not None:
        if input_semantics not in {"includes_cache", "excludes_cache"}:
            raise ValueError("cost estimation requires explicit cache semantics")
        for name in ("uncached_input", "cached_input", "output"):
            value = rates.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("rates need nonnegative USD per million tokens")
    with zipfile.ZipFile(wheel) as z:
        files = {n: sha(z.read(n)) for n in z.namelist() if n.startswith("ctx/") and not n.endswith("/")}
    if "ctx/sdk.py" not in files or "ctx/data/ctx_agy.py" not in files:
        raise ValueError("wheel does not contain the tool runtime and provider shim")
    return {"schema": "ctx.workflow-ablation/v1", "source_sha": source_sha,
        "wheel_sha256": sha(wheel.read_bytes()), "runtime_files": files,
        "sdk_version": sdk_version, "model": model, "profiles": PROFILES,
        "repeats": repeats, "timeout_seconds": timeout, "tasks": tasks,
        "rates_usd_per_million": rates, "input_semantics": input_semantics,
        "context_reuse": False, "tool_profile": "full"}


def verify_runtime(lock, python):
    """Refuse a checkout/editable install or another wheel before paid work."""
    script = """import hashlib,importlib.metadata,json,pathlib,ctx
root=pathlib.Path(ctx.__file__).parent
files={'ctx/'+str(p.relative_to(root)).replace('\\\\','/'): hashlib.sha256(p.read_bytes()).hexdigest()
       for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
print(json.dumps({'sdk_version':importlib.metadata.version('google-antigravity'),'files':files}))
"""
    p = subprocess.run([str(python), "-c", script], check=True, capture_output=True, text=True, timeout=30)
    actual = json.loads(p.stdout)
    if actual["sdk_version"] != lock["sdk_version"] or actual["files"] != lock["runtime_files"]:
        raise ValueError("installed runtime or provider SDK differs from the pinned wheel/profile")
    if lock["profiles"] != PROFILES or lock.get("context_reuse") is not False:
        raise ValueError("profile definitions differ; freeze a new lock")


def verify_corpus(tasks):
    for task in tasks:
        current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=task["dir"], text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=task["dir"], text=True).strip()
        if current != task["corpus_commit"] or dirty:
            raise ValueError("corpus revision or contents differ from the lock")


def estimated_cost(usage, rates):
    values = usage.get("derived", {})
    counters = [values.get(k) for k in ("uncached_input", "cached_input", "candidate_output", "thinking_output")]
    if rates is None or usage.get("accounting_error") or any(type(v) is not int or v < 0 for v in counters):
        return None
    a, b, c, d = counters
    return (a * rates["uncached_input"] + b * rates["cached_input"] + (c+d) * rates["output"]) / 1_000_000


def summarize(lock, rows):
    expected = {(t["id"], arm, r) for t in lock["tasks"] for arm in lock["profiles"]
                for r in range(1, lock["repeats"] + 1)}
    actual = [(r["task_id"], r["arm"], r["repeat"]) for r in rows]
    if len(set(actual)) != len(actual) or set(actual) - expected:
        raise ValueError("duplicate or unexpected trial cells")
    arms = {}
    for arm in lock["profiles"]:
        subset = [r for r in rows if r["arm"] == arm]
        resolved = sum(r.get("resolved") is True for r in subset)
        costs = [r.get("estimated_model_usd") for r in subset]
        total = sum(costs) if subset and all(c is not None for c in costs) else None
        arms[arm] = {"attempts": len(subset), "resolved_attempts": resolved,
            "estimated_model_usd": total, "known_estimated_subtotal_usd": sum(c for c in costs if c is not None),
            "unknown_cost_attempts": sum(c is None for c in costs),
            "estimated_usd_per_resolved_attempt": total / resolved if total is not None and resolved else None,
            "agent_wall_seconds": sum(r.get("agent_wall_seconds", 0) for r in subset),
            "model_requests": sum(r["model_requests"] for r in subset) if subset and all(r.get("model_requests") is not None for r in subset) else None,
            "custom_tool_calls": sum(r["custom_tool_calls"] for r in subset) if subset and all(r.get("custom_tool_calls") is not None for r in subset) else None,
            "delivered_bytes": sum(r["delivered_bytes"] for r in subset) if subset and all(r.get("delivered_bytes") is not None for r in subset) else None,
            "failed_attempts": sum(r.get("resolved") is not True for r in subset)}
    return {"schema": "ctx.workflow-ablation-report/v1", "complete": set(actual) == expected,
        "missing_cells": len(expected - set(actual)), "arms": arms,
        "claim": "internal_matched_repeats_not_billed_cost_or_a_public_resolve_rate"}


def run_live(lock, python, out, work_root):
    verify_runtime(lock, python)
    if not os.environ.get("GEMINI_API_KEY"):
        raise ValueError("GEMINI_API_KEY is required for an explicitly live run")
    if work_root.resolve().is_relative_to(ROOT):
        raise ValueError("benchmark workspaces must be outside the implementation checkout")
    adapter = load_adapter("deepswe")
    # Check the pinned corpus before preparing a fixture or invoking the model.
    verify_corpus(lock["tasks"])
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    for repeat in range(1, lock["repeats"] + 1):
        for n, task in enumerate(lock["tasks"]):
            arms = list(lock["profiles"])
            offset = (repeat - 1 + n) % len(arms)
            for arm in arms[offset:] + arms[:offset]:
                tag = f"{sha(task['id'].encode())[:12]}-{arm}-r{repeat}"
                workdir = work_root / tag
                workdir.mkdir(parents=True, exist_ok=False)
                rec = {"task_id": task["id"], "arm": arm, "repeat": repeat,
                       "resolved": False, "estimated_model_usd": None, "provenance": "live"}
                started = time.monotonic()
                try:
                    prompt = adapter.prepare(task, workdir)
                    env = {**os.environ, **adapter.session_env(task, workdir),
                           "CTX_STATE_HOME": str((out / "state" / tag).resolve())}
                    argv = [str(python), "-m", "ctx.data.ctx_agy", "-p", prompt, "--json",
                        "--tool-profile", "full", "--model", lock["model"],
                        "--timeout", str(lock["timeout_seconds"]), "--usage-input-semantics", lock["input_semantics"],
                        *lock["profiles"][arm]]
                    started = time.monotonic()
                    proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, text=True,
                                          timeout=lock["timeout_seconds"] + 30)
                    rec["agent_wall_seconds"] = time.monotonic() - started
                    (out / (tag + ".stdout")).write_text(proc.stdout)
                    (out / (tag + ".stderr")).write_text(proc.stderr)
                    doc = json.loads(proc.stdout.strip().splitlines()[-1])
                    rec.update(usage=doc.get("usage"), model_requests=doc.get("model_requests"),
                        custom_tool_calls=doc.get("custom_tool_calls", 0), delivered_bytes=doc.get("delivered_bytes", 0),
                        session_error=bool(doc.get("error") or doc.get("timed_out") or proc.returncode),
                        estimated_model_usd=estimated_cost(doc.get("usage") or {}, lock["rates_usd_per_million"]))
                    rec.update(adapter.grade(task, workdir))
                except Exception as exc:
                    rec.update(error=type(exc).__name__, elapsed_before_error_seconds=time.monotonic()-started)
                rows.append(rec)
                payload = {"lock": lock, "results": rows, "report": summarize(lock, rows)}
                (out / "results.partial.json").write_text(json.dumps(payload, indent=2) + "\n")
    (out / "results.partial.json").replace(out / "results.json")
    return summarize(lock, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("freeze")
    p.add_argument("--wheel", type=Path, required=True)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--sdk-version", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--ids", required=True)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--timeout", type=float, default=2400)
    p.add_argument("--rates", type=Path, help="explicit JSON USD/million rates: uncached_input, cached_input, output")
    p.add_argument("--input-semantics", choices=("unknown", "includes_cache", "excludes_cache"), default="unknown")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("run")
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("--python", type=Path, required=True, help="isolated venv with the exact wheel and provider SDK")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--work-root", type=Path, required=True)
    p = sub.add_parser("report")
    p.add_argument("results", type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        if head != args.source_sha or dirty:
            raise ValueError("freeze from the clean source commit used to build the wheel")
        tasks = load_adapter("deepswe").load(0, tasks=str(args.tasks.resolve()), ids=args.ids)
        verify_corpus(tasks)
        lock = freeze(args.wheel, source_sha=args.source_sha, sdk_version=args.sdk_version,
            model=args.model, tasks=tasks, repeats=args.repeats, timeout=args.timeout,
            rates=json.loads(args.rates.read_text()) if args.rates else None, input_semantics=args.input_semantics)
        args.out.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    elif args.action == "run":
        print(json.dumps(run_live(json.loads(args.lock.read_text()), args.python.resolve(),
                                  args.out.resolve(), args.work_root.resolve()), indent=2))
    else:
        data = json.loads(args.results.read_text())
        print(json.dumps(summarize(data["lock"], data["results"]), indent=2))


if __name__ == "__main__":
    main()
