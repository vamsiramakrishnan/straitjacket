"""Vibe-Code-Bench-STYLE local analog, run through our orchestrator.

NOT the official Vals AI Vibe Code Bench — that benchmark's tasks, grader
(a Browser-Use agent), and OpenHands+Docker environment are proprietary and
access-gated. This is a faithful *analog*: natural-language web-app specs, built
by our model-routing orchestrator, graded by a real headless-Chromium
(Playwright) UI test, scored as the fraction of substeps that pass — the same
shape as the real benchmark's "% of substeps" metric. Honest label: our numbers,
our tasks, not theirs.

Per task it exercises **our approach** end to end:
  1. Route (ctx.orchestrator.build_route_plan): a priced plan → node routing.
     plan → Claude Opus (`prefer:strong`); build → Claude Sonnet (real tools).
  2. Handoff via the CAS: the plan is frozen to a `checkpoint:` the build reads.
  3. Build: `claude -p` with acceptEdits + a tool allowlist builds the app in a
     scratch dir and writes ./start.sh (serves on $PORT).
  4. Grade: start the app, drive it with Playwright, score substeps.
  5. Bounded fix loop: failing substeps go back to the builder (closed loop).

Only Claude has real file/exec tools in this environment, so Claude is the
builder; a `--planner gemini` flag routes the (text-only) plan node to Gemini to
show cross-vendor planning. Costs real money and minutes per task.

Run:  GEMINI_API_KEY=... python evals/vibecode/harness.py --task counter
      python evals/vibecode/harness.py --all --fix-rounds 2
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent / "src"))

from ctx.checkpoint import create_checkpoint, show_checkpoint  # noqa: E402
from ctx.hosts import detect_all  # noqa: E402
from ctx.orchestrator import build_route_plan, render_route_plan  # noqa: E402
from ctx.pricing import cost_usd  # noqa: E402
from ctx.store import Store  # noqa: E402
from ctx.workspace import resolve_workspace  # noqa: E402

TASKS_DIR = ROOT / "tasks"
_CHROME = next(
    (str(p) for p in [
        Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome"),
    ] if p.exists()),
    None,
) or next((str(p) for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")), None)

USAGE: list[dict] = []  # per-task model calls; reset each task


# ------------------------------------------------------------- model launchers
def _gemini(model: str, prompt: str, timeout: float) -> tuple[int, str]:
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read())
        text = doc["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:  # noqa: BLE001
        return 1, f"gemini error: {e}"
    u = doc.get("usageMetadata", {})
    USAGE.append({"engine": f"antigravity/{model}", "input": u.get("promptTokenCount", 0),
                  "output": u.get("candidatesTokenCount", 0)})
    return 0, text


def _claude(model: str, prompt: str, cwd: Path, timeout: float, tools: bool) -> tuple[int, str]:
    argv = ["claude", "-p", prompt, "--model", model, "--output-format", "json"]
    if tools:
        argv += ["--permission-mode", "acceptEdits",
                 "--allowedTools", "Edit", "Write", "MultiEdit", "Read", "Bash"]
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"claude error: {e}"
    out = proc.stdout.strip()
    doc = None
    if out:
        try:
            doc = json.loads(out)
        except json.JSONDecodeError:
            i = out.rfind("{")
            doc = None if i < 0 else _try(out[i:])
    if doc is None:
        return proc.returncode or 1, proc.stdout
    u = doc.get("usage", {})
    USAGE.append({"engine": f"claude/{model}", "input": u.get("input_tokens", 0),
                  "output": u.get("output_tokens", 0), "cost_usd": doc.get("total_cost_usd")})
    return (0 if not doc.get("is_error") else 1), doc.get("result", "")


def _try(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _agy_python() -> str | None:
    """The Python interpreter of a venv with google-antigravity installed."""
    cand = os.environ.get("CTX_AGY_PYTHON")
    for p in ([cand] if cand else []) + ["/tmp/agy-venv/bin/python",
                                          str(ROOT.parent.parent / ".agy-venv/bin/python")]:
        if p and Path(p).exists():
            return p
    return None


def _agy_build(model: str, prompt: str, build_dir: Path, timeout: float) -> bool:
    """Build via the Antigravity SDK agent (real file+shell tools) in build_dir.
    Runs agy_build.py in the SDK venv; records billed Gemini tokens."""
    py = _agy_python()
    if py is None:
        print("  [antigravity] no SDK venv found (set CTX_AGY_PYTHON); build skipped")
        return False
    pf = build_dir / ".build_prompt.txt"
    pf.write_text(prompt)
    try:
        proc = subprocess.run(
            [py, str(ROOT / "agy_build.py"), "--dir", str(build_dir),
             "--model", model, "--prompt-file", str(pf), "--timeout", str(timeout)],
            capture_output=True, text=True, timeout=timeout + 60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  [antigravity] SDK error: {e}")
        return False
    doc = _try(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    if doc is None:
        print(f"  [antigravity] no usage returned; stderr: {proc.stderr[-300:]}")
        return proc.returncode == 0
    USAGE.append({"engine": f"antigravity/{model}", "input": doc.get("input", 0),
                  "output": doc.get("output", 0)})
    return proc.returncode == 0


# --------------------------------------------------------------- app lifecycle
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def _start_app(build_dir: Path, port: int):
    start = build_dir / "start.sh"
    if not start.exists():
        return None
    env = {**os.environ, "PORT": str(port), "APP_PORT": str(port)}
    proc = subprocess.Popen(
        ["bash", "start.sh"], cwd=build_dir, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return proc if _wait_port(port) else (_kill(proc) or None)


def _kill(proc):
    if proc and proc.poll() is None:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    return None


# ------------------------------------------------------------------- grading
def _load_check(task: str):
    path = TASKS_DIR / task / "check.py"
    spec = importlib.util.spec_from_file_location(f"vibecheck_{task}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.check


def _grade(build_dir: Path, task: str, port: int) -> list[tuple[str, bool]]:
    from playwright.sync_api import sync_playwright

    proc = _start_app(build_dir, port)
    if proc is None:
        return [("app starts and serves on $PORT", False)]
    try:
        check = _load_check(task)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=_CHROME)
            page = browser.new_page()
            try:
                steps = check(page, f"http://127.0.0.1:{port}")
            finally:
                browser.close()
        return [("app starts and serves on $PORT", True), *steps]
    finally:
        _kill(proc)


# --------------------------------------------------------------- build (routed)
def _route(spec: str, planner: str, builder: str, builder_model: str, ws):
    """Our orchestrator decides who does what: plan → strong flagship (or the
    cheap Gemini planner), build → Claude Sonnet or the Antigravity SDK agent.
    Returns the priced RoutePlan (display/pricing; execution is in _build_app)."""
    which = lambda b: f"/usr/bin/{b}" if b in ("claude", "antigravity") else None  # noqa: E731
    hosts = [h for h in detect_all(which=which) if h.installed and h.harnessable]
    plan_node = ({"host": "antigravity", "model": "gemini-3.5-flash-lite", "min_tier": "economy"}
                 if planner == "gemini"
                 else {"host": "claude", "model": "claude-opus-4.8", "min_tier": "frontier", "prefer": "strong"})
    build_node = ({"host": "antigravity", "model": builder_model}
                  if builder == "antigravity"
                  else {"host": "claude", "model": "claude-sonnet-4.6"})
    raw = {"nodes": [
        {"id": "plan", "goal": "design the app", "role": "plan", "deps": [],
         "est_input_tokens": 1500, "est_output_tokens": 1200, **plan_node},
        {"id": "build", "goal": "build the app", "role": "implement", "min_tier": "standard",
         "deps": ["plan"], "est_input_tokens": 6000, "est_output_tokens": 12000, **build_node},
    ]}
    return build_route_plan(spec, raw, hosts, ws.config.orchestrate)


_APP_CONTRACT = (
    "Build the app in the CURRENT directory. Serve it over HTTP on the port in "
    "the env var $PORT (read it at runtime). Write an executable ./start.sh that "
    "starts the server on $PORT and blocks (no build step inside it). PREFER the "
    "language standard library / zero external dependencies so it starts fast and "
    "offline; if you must install deps, do it NOW during the build. A single "
    "static index.html + a tiny server is fine. Make the Acceptance bullets true."
)


def _build_app(ws, plan, spec: str, build_dir: Path, timeout: float, builder: str) -> str | None:
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    plan_asn = next(a for a in plan.assigned if a.node.id == "plan")
    build_asn = next(a for a in plan.assigned if a.node.id == "build")

    # 1) plan (text only, no tools) → checkpoint
    plan_prompt = (
        "You are the PLANNER. Read this web-app spec and produce a terse build "
        "plan: stack choice (prefer stdlib), file list, and how each Acceptance "
        "bullet will be satisfied. Output the plan only; write no files.\n\n" + spec
    )
    ph, pm = plan_asn.host, plan_asn.model
    if ph.name == "antigravity":
        _, ptext = _gemini(pm.launch_id, plan_prompt, 180)
    else:
        _, ptext = _claude(pm.launch_id, plan_prompt, build_dir, 300, tools=False)
    cp_id, _ = create_checkpoint(store, ws, goal="build plan", state=ptext[:1500])
    plan_doc = show_checkpoint(store, ws, f"checkpoint:{cp_id[:12]}")

    # 2) build (real tools) reading the plan checkpoint
    build_prompt = (
        "You are the BUILDER. Implement this spec as a working web app.\n\n"
        + spec + "\n\n--- plan from the upstream checkpoint ---\n" + plan_doc
        + "\n\n" + _APP_CONTRACT
    )
    if builder == "antigravity":
        ok = _agy_build(build_asn.model.launch_id, build_prompt, build_dir, timeout)
    else:
        code, _ = _claude(build_asn.model.launch_id, build_prompt, build_dir, timeout, tools=True)
        ok = code == 0
    _chmod_start(build_dir)
    return f"checkpoint:{cp_id[:12]}" if ok else None


def _chmod_start(build_dir: Path):
    s = build_dir / "start.sh"
    if s.exists():
        s.chmod(0o755)


def _fix(build_dir: Path, spec: str, failures: list[str], timeout: float,
         builder: str, builder_model: str):
    prompt = (
        "The app you built has FAILING acceptance checks. Fix the app in the "
        "current directory so they pass; keep ./start.sh serving on $PORT.\n\n"
        "Failing checks:\n" + "\n".join(f"- {f}" for f in failures)
        + "\n\nSpec (for reference):\n" + spec
    )
    if builder == "antigravity":
        ok = _agy_build(builder_model, prompt, build_dir, timeout)
    else:
        code, _ = _claude("sonnet", prompt, build_dir, timeout, tools=True)
        ok = code == 0
    _chmod_start(build_dir)
    return ok


# ------------------------------------------------------------------- per task
def run_task(task: str, planner: str, builder: str, builder_model: str,
             fix_rounds: int, build_timeout: float) -> dict:
    USAGE.clear()
    spec = (TASKS_DIR / task / "spec.md").read_text()
    build_dir = Path(tempfile.mkdtemp(prefix=f"vibecode-{task}-"))
    _git_init(build_dir)
    ws = resolve_workspace(str(build_dir))
    port = _free_port()

    plan = _route(spec, planner, builder, builder_model, ws)
    print(render_route_plan(plan))
    print(f"\n--- building '{task}' in {build_dir} (builder={builder}) ---")
    _build_app(ws, plan, spec, build_dir, build_timeout, builder)

    steps = _grade(build_dir, task, port)
    rounds = 0
    while fix_rounds > 0 and any(not ok for _, ok in steps):
        fails = [label for label, ok in steps if not ok]
        print(f"  fix round {rounds + 1}: {len(fails)} failing → re-building")
        _fix(build_dir, spec, fails, build_timeout, builder, builder_model)
        steps = _grade(build_dir, task, port)
        rounds += 1
        fix_rounds -= 1

    passed = sum(1 for _, ok in steps if ok)
    score = passed / len(steps) if steps else 0.0
    cost = _cost()
    shutil.rmtree(build_dir, ignore_errors=True)
    return {"task": task, "score": score, "passed": passed, "total": len(steps),
            "steps": steps, "fix_rounds": rounds, "cost_usd": cost}


def _cost() -> float:
    total = 0.0
    for r in USAGE:
        c = r.get("cost_usd")
        if c is None:
            c = cost_usd({"input": r["input"], "output": r["output"]}, r["engine"].split("/", 1)[1])
        total += c
    return total


def _git_init(d: Path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, env=env)


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", help="single task name (dir under tasks/)")
    ap.add_argument("--all", action="store_true", help="run every task")
    ap.add_argument("--planner", choices=["claude", "gemini"], default="claude")
    ap.add_argument("--builder", choices=["claude", "antigravity"], default="claude",
                    help="claude = claude -p (tools); antigravity = the Antigravity SDK agent")
    ap.add_argument("--builder-model", default="gemini-3.6-flash",
                    help="model for the Antigravity SDK builder")
    ap.add_argument("--fix-rounds", type=int, default=1)
    ap.add_argument("--build-timeout", type=float, default=900.0)
    ns = ap.parse_args()
    need_gem = ns.planner == "gemini" or ns.builder == "antigravity"
    if need_gem and not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not set", file=sys.stderr)
        return 2
    if ns.builder == "antigravity" and _agy_python() is None:
        print("Antigravity SDK venv not found. Create one:\n"
              "  python -m venv .agy-venv && .agy-venv/bin/pip install google-antigravity\n"
              "or set CTX_AGY_PYTHON to a venv python that has google-antigravity.",
              file=sys.stderr)
        return 2
    if _CHROME is None:
        print("no chromium found under /opt/pw-browsers", file=sys.stderr)
        return 2

    tasks = ([ns.task] if ns.task else
             sorted(p.name for p in TASKS_DIR.iterdir() if (p / "spec.md").exists()))
    results = []
    for t in tasks:
        print("=" * 70, f"\nTASK: {t}\n" + "=" * 70)
        results.append(run_task(t, ns.planner, ns.builder, ns.builder_model,
                                ns.fix_rounds, ns.build_timeout))

    print("\n" + "=" * 70, "\nVIBE-CODE ANALOG — RESULTS\n" + "=" * 70)
    for r in results:
        bar = "".join("✓" if ok else "✗" for _, ok in r["steps"])
        print(f"  {r['task']:10} {r['passed']}/{r['total']} ({r['score']*100:.0f}%) "
              f"[{bar}] fix={r['fix_rounds']} ${r['cost_usd']:.3f}")
    avg = sum(r["score"] for r in results) / len(results) if results else 0
    tot = sum(r["cost_usd"] for r in results)
    print(f"  {'AVERAGE':10} {avg*100:.0f}%   total ${tot:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
