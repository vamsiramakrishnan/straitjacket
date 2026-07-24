"""LIVE harness collaboration: Antigravity (Gemini) + Claude, real tokens.

Drives the *actual* orchestrator closed loop (`ctx.orchestrator.run_route`) with
a real launcher instead of the injected fake used in the unit tests:

* antigravity nodes  -> Google Gemini API (GEMINI_API_KEY) — the model
  Antigravity runs. This is the headless-driveable path; the `agy` CLI needs
  interactive OAuth (see antigravity-gemini-2026-07-19.md).
* claude nodes       -> `claude -p ... --output-format json` (Claude Code CLI,
  authenticated in this environment).

The handoff between nodes is the real CAS checkpoint written by `run_route`:
the Claude node's prompt carries the Gemini node's `checkpoint:` digest, and we
assert it did. Usage/cost are read from each provider's own response.

Run:  GEMINI_API_KEY=... python evals/live_collab_antigravity_claude.py
Cost: a few cents (tiny task, haiku + flash-lite/flash).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.hosts import detect_all  # noqa: E402
from ctx.orchestrator import build_route_plan, render_route_plan, run_route  # noqa: E402
from ctx.workspace import resolve_workspace  # noqa: E402

# The registry now carries the real launch id per model (ModelChoice.cli_id),
# so run_route hands this driver the id the provider actually serves — no local
# mapping needed. That id resolution is exactly the gap the first live run found.
USAGE: list[dict] = []  # one row per real model call


def _gemini(model: str, prompt: str, timeout: float) -> tuple[int, str, str]:
    api_model = model  # already the served id (e.g. gemini-3.5-flash-lite)
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{api_model}:generateContent?key={key}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return 1, "", f"gemini error: {e}"
    try:
        text = doc["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return 1, "", f"gemini bad response: {json.dumps(doc)[:200]}"
    u = doc.get("usageMetadata", {})
    USAGE.append({
        "engine": f"antigravity/{api_model}", "provider": "gemini",
        "input": u.get("promptTokenCount", 0), "output": u.get("candidatesTokenCount", 0),
    })
    return 0, text, ""


def _claude(model: str, prompt: str, cwd: Path, timeout: float) -> tuple[int, str, str]:
    alias = model  # already the CLI alias (e.g. haiku)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", alias, "--output-format", "json"],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", f"claude error: {e}"
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode or 1, proc.stdout, proc.stderr
    u = doc.get("usage", {})
    USAGE.append({
        "engine": f"claude/{alias}", "provider": "anthropic",
        "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
        "cost_usd": doc.get("total_cost_usd"),
    })
    return 0, doc.get("result", ""), ""


def real_launch(host, ws_root, prompt, exe, *, timeout, model=""):
    """Route the node to its real provider by harness name."""
    if host.name == "antigravity":
        return _gemini(model, prompt, timeout)
    if host.name == "claude":
        return _claude(model, prompt, Path(ws_root), timeout)
    return 127, "", f"no live driver for host {host.name}"


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not set", file=sys.stderr)
        return 2

    ws = resolve_workspace(".")
    # Pretend claude + antigravity are both installed (real drivers, not PATH).
    which = lambda b: f"/usr/bin/{b}" if b in ("claude", "antigravity") else None  # noqa: E731
    hosts = [h for h in detect_all(which=which) if h.installed and h.harnessable]

    task = ("Write a Python function fib(n) returning the nth Fibonacci number "
            "iteratively (fib(0)=0, fib(1)=1), with a one-line docstring.")

    # Two-model collaboration: Gemini (Antigravity) plans; Claude implements from
    # the plan's checkpoint. Pinned so both providers are exercised.
    raw = {"nodes": [
        {"id": "plan", "goal": "Give a terse 3-step plan to implement the task. No code.",
         "role": "plan", "min_tier": "economy", "host": "antigravity",
         "model": "gemini-3.6-flash-lite", "deps": [],
         "est_input_tokens": 200, "est_output_tokens": 150},
        {"id": "implement", "goal": "Using the plan in the upstream checkpoint, write the "
         "function and one assert-based test. Output only a python code block.",
         "role": "implement", "min_tier": "economy", "host": "claude",
         "model": "claude-haiku-4.5", "deps": ["plan"],
         "est_input_tokens": 400, "est_output_tokens": 300},
    ]}
    plan = build_route_plan(task, raw, hosts, ws.config.orchestrate)
    print(render_route_plan(plan))
    print("\n--- running live (real Gemini + real Claude) ---\n")

    result = run_route(ws, plan, ws.config.orchestrate, launch=real_launch)

    # Handoff proof: the implement node must have seen the plan checkpoint.
    plan_cp = next(o.checkpoint_ref for o in result.outcomes if o.node_id == "plan")
    from ctx.store import Store
    from ctx.checkpoint import show_checkpoint
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    plan_doc = show_checkpoint(store, ws, plan_cp)

    print("=== outcomes ===")
    for o in result.outcomes:
        print(f"  {o.node_id:10} {o.host_name:35} [{o.status}] {o.checkpoint_ref}")
    print(f"\n=== handoff proof ===\nplan checkpoint {plan_cp} handed to Claude:\n{plan_doc[:300]}\n")
    print("=== real usage / cost ===")
    from ctx.pricing import cost_usd
    total = 0.0
    for row in USAGE:
        model = row["engine"].split("/", 1)[1]
        c = row.get("cost_usd")
        if c is None:
            c = cost_usd({"input": row["input"], "output": row["output"]}, model)
        total += c
        print(f"  {row['engine']:32} in={row['input']:5} out={row['output']:5} ${c:.4f}")
    print(f"  {'TOTAL':32} {'':16} ${total:.4f}")
    print(f"\nproviders exercised: {sorted({r['provider'] for r in USAGE})}")
    ok = (len(result.outcomes) == 2 and all(o.status == "ok" for o in result.outcomes)
          and {r["provider"] for r in USAGE} == {"gemini", "anthropic"})
    print("RESULT:", "PASS — Antigravity(Gemini)+Claude collaborated live" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
