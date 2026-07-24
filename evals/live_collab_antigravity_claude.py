"""LIVE harness collaboration on a REAL task: Antigravity (Gemini) plans,
Claude implements with its real tools, the test goes green.

Drives the *actual* orchestrator closed loop (`ctx.orchestrator.run_route`) with
a live launcher — no ANTHROPIC_API_KEY needed, Claude Code runs authenticated
as-is with its full Bash/Read/Edit tools:

* antigravity nodes -> Google Gemini API (GEMINI_API_KEY) — the model
  Antigravity runs (the `agy` CLI is OAuth-only; the API is the headless path).
* claude nodes       -> `claude -p … --output-format json` in the work dir,
  editing files and running the test itself.

A throwaway git repo holds a failing test. The route is:
  plan (Gemini, economy) -> implement (Claude, real tools) ⇐ plan
The handoff is the real CAS checkpoint. The deliverable is verifiable: after the
run we run pytest in the scratch repo and require it green.

Run:  GEMINI_API_KEY=... python evals/live_collab_antigravity_claude.py
Cost: ~10-20 cents (one Sonnet agentic run + a Gemini plan).
Note: the Claude node runs with --dangerously-skip-permissions so it can edit
and run pytest unattended. That is safe here ONLY because it runs inside a
mktemp throwaway git repo, never your real workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.checkpoint import show_checkpoint  # noqa: E402
from ctx.hosts import detect_all  # noqa: E402
from ctx.orchestrator import build_route_plan, render_route_plan, run_route  # noqa: E402
from ctx.store import Store  # noqa: E402
from ctx.workspace import resolve_workspace  # noqa: E402

USAGE: list[dict] = []  # one row per real model call


def _gemini(model: str, prompt: str, timeout: float) -> tuple[int, str, str]:
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read())
        text = doc["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:  # noqa: BLE001
        return 1, "", f"gemini error: {e}"
    u = doc.get("usageMetadata", {})
    USAGE.append({"engine": f"antigravity/{model}", "provider": "gemini",
                  "input": u.get("promptTokenCount", 0), "output": u.get("candidatesTokenCount", 0)})
    return 0, text, ""


def _claude(model: str, prompt: str, cwd: Path, timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            # acceptEdits + a narrow allowlist lets Claude edit and run the test
            # unattended. --dangerously-skip-permissions is refused under root,
            # so we grant exactly the tools the implement node needs instead.
            ["claude", "-p", prompt, "--model", model, "--output-format", "json",
             "--permission-mode", "acceptEdits",
             "--allowedTools", "Edit", "Write", "MultiEdit", "Read",
             "Bash(python*)", "Bash(pytest*)"],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", f"claude error: {e}"
    doc = None
    with_ = proc.stdout.strip()
    if with_:
        try:
            doc = json.loads(with_)
        except json.JSONDecodeError:
            i = with_.rfind("{")
            if i != -1:
                try:
                    doc = json.loads(with_[i:])
                except json.JSONDecodeError:
                    doc = None
    if doc is None:
        return proc.returncode or 1, proc.stdout, proc.stderr
    u = doc.get("usage", {})
    USAGE.append({"engine": f"claude/{model}", "provider": "anthropic",
                  "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                  "cost_usd": doc.get("total_cost_usd")})
    return 0, doc.get("result", ""), ""


def real_launch(host, ws_root, prompt, exe, *, timeout, model=""):
    if host.name == "antigravity":
        return _gemini(model, prompt, timeout)
    if host.name == "claude":
        return _claude(model, prompt, Path(ws_root), timeout)
    return 127, "", f"no live driver for host {host.name}"


FAILING_TEST = '''from strings import longest_run
def test_longest_run():
    assert longest_run("aabbbccd") == ("b", 3)
    assert longest_run("") == ("", 0)
    assert longest_run("xyz") in (("x", 1), ("y", 1), ("z", 1))
'''
STUB = "def longest_run(s):\n    raise NotImplementedError\n"


def _make_scratch() -> Path:
    d = Path(tempfile.mkdtemp(prefix="ctx-live-collab-"))
    (d / "strings.py").write_text(STUB)
    (d / "test_strings.py").write_text(FAILING_TEST)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "failing"], cwd=d, check=True, env=env)
    return d


def _pytest_green(d: Path) -> bool:
    r = subprocess.run(["python", "-m", "pytest", "-q"], cwd=d, capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not set", file=sys.stderr)
        return 2

    scratch = _make_scratch()
    try:
        assert not _pytest_green(scratch), "fixture should start red"
        ws = resolve_workspace(str(scratch))
        which = lambda b: f"/usr/bin/{b}" if b in ("claude", "antigravity") else None  # noqa: E731
        hosts = [h for h in detect_all(which=which) if h.installed and h.harnessable]

        task = (f"In this repo, `strings.longest_run(s)` must return the (char, count) of the "
                f"longest run of a single repeated character, ('' , 0) for empty input. "
                f"The failing test:\n{FAILING_TEST}")

        raw = {"nodes": [
            {"id": "plan", "goal": "Give a terse 3-step plan to implement longest_run. No code.",
             "role": "plan", "min_tier": "economy", "host": "antigravity",
             "model": "gemini-3.6-flash-lite", "deps": [],
             "est_input_tokens": 300, "est_output_tokens": 150},
            {"id": "implement", "goal": "Using the plan in the upstream checkpoint, edit "
             "strings.py so `python -m pytest -q` passes. Run pytest yourself to confirm.",
             "role": "implement", "min_tier": "standard", "host": "claude",
             "model": "claude-sonnet-4.6", "deps": ["plan"],
             "est_input_tokens": 800, "est_output_tokens": 600},
        ]}
        plan = build_route_plan(task, raw, hosts, ws.config.orchestrate)
        print(render_route_plan(plan))
        print("\n--- running live (Gemini plans · Claude implements with real tools) ---\n")

        result = run_route(ws, plan, ws.config.orchestrate, launch=real_launch)

        plan_cp = next(o.checkpoint_ref for o in result.outcomes if o.node_id == "plan")
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        plan_doc = show_checkpoint(store, ws, plan_cp)

        green = _pytest_green(scratch)
        impl = (scratch / "strings.py").read_text()

        print("=== outcomes ===")
        for o in result.outcomes:
            print(f"  {o.node_id:10} {o.host_name:32} [{o.status}] {o.checkpoint_ref}")
        print(f"\n=== handoff proof (plan {plan_cp} → Claude) ===\n{plan_doc[:260]}\n")
        print("=== Claude's edit (strings.py) ===")
        print("  " + "\n  ".join(impl.strip().splitlines()[:8]))
        print(f"\n=== verifiable deliverable ===\n  pytest in scratch repo: {'GREEN ✓' if green else 'RED ✗'}")

        print("\n=== real usage / cost ===")
        from ctx.pricing import cost_usd
        total = 0.0
        for row in USAGE:
            model = row["engine"].split("/", 1)[1]
            c = row.get("cost_usd")
            if c is None:
                c = cost_usd({"input": row["input"], "output": row["output"]}, model)
            total += c
            print(f"  {row['engine']:34} in={row['input']:6} out={row['output']:6} ${c:.4f}")
        print(f"  {'TOTAL':34} {'':18} ${total:.4f}")

        providers = {r["provider"] for r in USAGE}
        ok = (green and all(o.status == "ok" for o in result.outcomes)
              and providers == {"gemini", "anthropic"})
        print(f"\nproviders: {sorted(providers)}")
        print("RESULT:", "PASS — Gemini planned, Claude implemented, test green" if ok else "INCOMPLETE")
        return 0 if ok else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
