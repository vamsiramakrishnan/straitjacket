#!/usr/bin/env python3
"""Iterative vibe-code A/B: one strong model solo vs the routing orchestrator.

The single-shot harness (`harness.py`) asks a model to build an app from a
frozen spec. Real vibe-coding is not like that: the design gets **reshaped
mid-build**, and the second turn has to hold everything the first turn built
while reversing the parts the reviewer changed their mind about. That is the
expensive turn, and it is the one this harness measures.

The task (`tasks/triage/`) is deliberately hard and two-phase:

  phase 1  `spec.md`   an incident triage console — table, single-select
                       severity chips, click-to-sort, detail panel, URL-hash
                       sharing.  Graded by `check.check`.
  phase 2  `amend.md`  the design review reshapes it — the table becomes a
                       three-lane status board, the chips become **multi**-
                       select (reversing phase 1), the panel becomes a real
                       modal (role/aria-modal/focus/Escape), plus a persisted
                       theme toggle — while the hash sharing, seed data and
                       live counts must survive.  Graded by `check.check_phase2`,
                       which re-checks the phase-1 behaviours the amendment did
                       *not* contradict, so "rewrite it from scratch" does not
                       get a free pass.

Arms (same task, same grader, same fix-round budget, same throwaway repo):

  solo          one Claude **Opus** agent does plan+build itself, both phases.
                No routing, no handoff — the "just use the strong model" arm.
  orchestrated  `ctx.orchestrator` routes each phase: plan → Opus
                (`prefer:strong`, text only), build → Claude **Sonnet** with
                real tools, with the plan handed over as a CAS `checkpoint:`
                rather than as raw prose.
  cross         plan → Opus, build → the **Antigravity SDK agent** (Gemini)
                with its own file+shell tools. Cross-vendor split.

Reported per arm: phase-1 score, phase-2 score (the reshape), fix rounds used,
and real billed cost per model from the shipped price table.

Run:
  python evals/vibecode/iterative_harness.py --arm solo --arm orchestrated
  python evals/vibecode/iterative_harness.py --arm cross --fix-rounds 1
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent / "src"))

import harness as H  # noqa: E402  (reuses the launchers, app lifecycle, pricing)
from ctx.checkpoint import create_checkpoint, show_checkpoint  # noqa: E402
from ctx.hosts import detect_all  # noqa: E402
from ctx.orchestrator import build_route_plan, render_route_plan  # noqa: E402
from ctx.store import Store  # noqa: E402
from ctx.workspace import resolve_workspace  # noqa: E402

TASKS_DIR = ROOT / "tasks"
ARMS = ("solo", "orchestrated", "cross")


# ------------------------------------------------------------------- grading
def _grade(build_dir: Path, task: str, port: int, fn: str) -> list[tuple[str, bool]]:
    """Start the app, drive it with the named grader, stop it."""
    from playwright.sync_api import sync_playwright

    proc = H._start_app(build_dir, port)
    if proc is None:
        return [("app starts and serves on $PORT", False)]
    try:
        path = TASKS_DIR / task / "check.py"
        import importlib.util

        spec = importlib.util.spec_from_file_location(f"vibecheck_{task}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        check = getattr(mod, fn)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=H._CHROME)
            page = browser.new_page()
            try:
                steps = check(page, f"http://127.0.0.1:{port}")
            finally:
                browser.close()
        return [("app starts and serves on $PORT", True), *steps]
    finally:
        H._kill(proc)


def _bar(steps):
    return "".join("✓" if ok else "✗" for _, ok in steps)


# ------------------------------------------------------------------- routing
def _route(goal: str, arm: str, ws, builder_model: str):
    """The priced route plan for one phase. `solo` is a single frontier node;
    the other arms split plan (frontier, prefer strong) from build."""
    which = lambda b: f"/usr/bin/{b}" if b in ("claude", "antigravity") else None  # noqa: E731
    hosts = [h for h in detect_all(which=which) if h.installed and h.harnessable]
    if arm == "solo":
        raw = {"nodes": [{
            "id": "build", "goal": goal, "role": "implement", "deps": [],
            "host": "claude", "model": "claude-opus-4.8", "min_tier": "frontier",
            "prefer": "strong", "est_input_tokens": 9000, "est_output_tokens": 14000,
        }]}
        return build_route_plan(goal, raw, hosts, ws.config.orchestrate)
    build_node = ({"host": "antigravity", "model": builder_model, "min_tier": "standard"}
                  if arm == "cross"
                  else {"host": "claude", "model": "claude-sonnet-4.6", "min_tier": "standard"})
    raw = {"nodes": [
        {"id": "plan", "goal": "design the reshape", "role": "plan", "deps": [],
         "host": "claude", "model": "claude-opus-4.8", "min_tier": "frontier",
         "prefer": "strong", "est_input_tokens": 2500, "est_output_tokens": 2000},
        {"id": "build", "goal": goal, "role": "implement", "deps": ["plan"],
         "est_input_tokens": 9000, "est_output_tokens": 14000, **build_node},
    ]}
    return build_route_plan(goal, raw, hosts, ws.config.orchestrate)


def _inventory(build_dir: Path, limit: int = 40) -> str:
    """A bounded description of what already exists — what the planner is
    allowed to see in phase 2 (it has no tools). Names + line counts only."""
    rows = []
    for p in sorted(build_dir.rglob("*")):
        if p.is_dir() or any(part.startswith(".") for part in p.relative_to(build_dir).parts):
            continue
        try:
            n = len(p.read_text(errors="replace").splitlines())
        except (OSError, UnicodeError):
            n = 0
        rows.append(f"  {p.relative_to(build_dir)} ({n} lines)")
        if len(rows) >= limit:
            rows.append("  …")
            break
    return "\n".join(rows) or "  (nothing built)"


# ---------------------------------------------------------------- build steps
def _build(arm: str, ws, plan, prompt: str, build_dir: Path, timeout: float,
           builder_model: str, plan_prompt: str | None) -> None:
    """Run one phase's build. Split arms plan first and hand the plan over as a
    CAS checkpoint; solo goes straight to the strong model."""
    if arm == "solo":
        H._claude("opus", prompt, build_dir, timeout, tools=True)
        H._chmod_start(build_dir)
        return

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    plan_asn = next(a for a in plan.assigned if a.node.id == "plan")
    build_asn = next(a for a in plan.assigned if a.node.id == "build")
    _, ptext = H._claude(plan_asn.model.launch_id, plan_prompt or prompt, build_dir,
                         300, tools=False)
    cp_id, _ = create_checkpoint(store, ws, goal="build plan", state=ptext[:1500])
    plan_doc = show_checkpoint(store, ws, f"checkpoint:{cp_id[:12]}")
    full = prompt + "\n\n--- plan from the upstream checkpoint ---\n" + plan_doc

    if arm == "cross":
        H._agy_build(build_asn.model.launch_id, full, build_dir, timeout)
    else:
        H._claude(build_asn.model.launch_id, full, build_dir, timeout, tools=True)
    H._chmod_start(build_dir)


def _fix(arm: str, failures: list[str], spec: str, build_dir: Path, timeout: float,
         builder_model: str) -> None:
    prompt = (
        "The app in the current directory has FAILING acceptance checks. Fix it "
        "so they pass; keep ./start.sh serving on $PORT.\n\nFailing checks:\n"
        + "\n".join(f"- {f}" for f in failures)
        + "\n\nSpec (for reference):\n" + spec
    )
    if arm == "solo":
        H._claude("opus", prompt, build_dir, timeout, tools=True)
    elif arm == "cross":
        H._agy_build(builder_model, prompt, build_dir, timeout)
    else:
        H._claude("sonnet", prompt, build_dir, timeout, tools=True)
    H._chmod_start(build_dir)


# ---------------------------------------------------------------- phase driver
def _phase(arm: str, ws, build_dir: Path, task: str, port: int, label: str,
           prompt: str, plan_prompt: str | None, grader: str, spec_ref: str,
           fix_rounds: int, timeout: float, builder_model: str) -> dict:
    plan = _route(f"{task}: {label}", arm, ws, builder_model)
    print(render_route_plan(plan))
    t0 = time.monotonic()
    print(f"--- {arm}/{label}: building in {build_dir} ---", flush=True)
    _build(arm, ws, plan, prompt, build_dir, timeout, builder_model, plan_prompt)

    steps = _grade(build_dir, task, port, grader)
    rounds = 0
    while fix_rounds > 0 and any(not ok for _, ok in steps):
        fails = [lab for lab, ok in steps if not ok]
        print(f"    fix round {rounds + 1}: {len(fails)} failing → re-building", flush=True)
        _fix(arm, fails, spec_ref, build_dir, timeout, builder_model)
        steps = _grade(build_dir, task, port, grader)
        rounds += 1
        fix_rounds -= 1
    passed = sum(1 for _, ok in steps if ok)
    print(f"    {label}: {passed}/{len(steps)} [{_bar(steps)}] "
          f"fix={rounds} {time.monotonic() - t0:.0f}s", flush=True)
    return {"phase": label, "passed": passed, "total": len(steps),
            "score": passed / len(steps) if steps else 0.0,
            "steps": [[lab, ok] for lab, ok in steps], "fix_rounds": rounds,
            "wall_s": round(time.monotonic() - t0, 1),
            "route": [f"{a.node.id} → {a.host.name}/{a.model.id}" for a in plan.assigned],
            "route_est_usd": round(sum(a.est_cost_usd for a in plan.assigned), 4)}


def run_arm(arm: str, task: str, fix_rounds: int, timeout: float,
            builder_model: str, keep: bool) -> dict:
    H.USAGE.clear()
    spec = (TASKS_DIR / task / "spec.md").read_text()
    amend = (TASKS_DIR / task / "amend.md").read_text()
    build_dir = Path(tempfile.mkdtemp(prefix=f"iter-{task}-{arm}-"))
    H._git_init(build_dir)
    ws = resolve_workspace(str(build_dir))
    port = H._free_port()

    print("=" * 72, f"\nARM: {arm}\n" + "=" * 72, flush=True)
    p1 = _phase(
        arm, ws, build_dir, task, port, "phase1",
        prompt="You are building a web app from this spec.\n\n" + spec + "\n\n" + H._APP_CONTRACT,
        plan_prompt=("You are the PLANNER. Read this web-app spec and produce a terse "
                     "build plan: stack choice (prefer stdlib), file list, and how each "
                     "Acceptance bullet will be satisfied. Output the plan only; write "
                     "no files.\n\n" + spec),
        grader="check", spec_ref=spec, fix_rounds=fix_rounds, timeout=timeout,
        builder_model=builder_model)

    reshape = (
        "The app in the CURRENT directory is your phase-1 build. A design review "
        "has now RESHAPED it. Apply the amendment below to the existing app: parts "
        "of phase 1 are superseded and must be replaced, not kept alongside. "
        "Everything phase 1 asked for that the amendment does not contradict must "
        "keep working.\n\n--- original phase-1 spec ---\n" + spec
        + "\n\n--- design review amendment (phase 2) ---\n" + amend
        + "\n\n" + H._APP_CONTRACT
    )
    p2 = _phase(
        arm, ws, build_dir, task, port, "phase2",
        prompt=reshape,
        plan_prompt=("You are the PLANNER. The app already exists; here is its file "
                     "inventory:\n" + _inventory(build_dir) + "\n\nA design review has "
                     "reshaped the design. Produce a terse change plan: which files "
                     "change, what is removed outright (superseded), what is added, and "
                     "how each phase-2 Acceptance bullet will be satisfied — including "
                     "the phase-1 behaviours that must survive. Output the plan only; "
                     "write no files.\n\n--- phase-1 spec ---\n" + spec
                     + "\n\n--- amendment ---\n" + amend),
        grader="check_phase2", spec_ref=spec + "\n\n" + amend, fix_rounds=fix_rounds,
        timeout=timeout, builder_model=builder_model)

    cost = H._cost()
    usage = [dict(u) for u in H.USAGE]
    if keep:
        print(f"  build dir kept: {build_dir}")
    else:
        shutil.rmtree(build_dir, ignore_errors=True)
    return {"arm": arm, "task": task, "phases": [p1, p2], "cost_usd": round(cost, 4),
            "usage": usage,
            "score": (p1["score"] + p2["score"]) / 2}


# ------------------------------------------------------------------- report
def render(results: list[dict]) -> str:
    lines = [
        "# Iterative vibe-code — solo frontier model vs the routing orchestrator",
        "",
        "Task `tasks/triage`: an incident triage console built from a spec, then "
        "**reshaped mid-build** by a design review that reverses part of what was "
        "already built (table → status board, single-select → multi-select chips, "
        "side panel → modal dialog) while the untouched phase-1 behaviours must "
        "survive. Graded by headless Chromium; score = fraction of substeps that pass.",
        "",
        "| arm | routing | phase 1 | phase 2 (reshape) | mean | fix rounds | billed |",
        "|---|---|--:|--:|--:|--:|--:|",
    ]
    for r in results:
        p1, p2 = r["phases"]
        route = "<br>".join(p2["route"])
        lines.append(
            f"| `{r['arm']}` | {route} | {p1['passed']}/{p1['total']} "
            f"({p1['score']*100:.0f}%) | {p2['passed']}/{p2['total']} "
            f"({p2['score']*100:.0f}%) | {r['score']*100:.0f}% | "
            f"{p1['fix_rounds']}+{p2['fix_rounds']} | ${r['cost_usd']:.3f} |"
        )
    lines.append("")
    for r in results:
        lines.append(f"### `{r['arm']}` — substeps")
        for p in r["phases"]:
            lines.append(f"- **{p['phase']}** `{_bar([(s[0], s[1]) for s in p['steps']])}` "
                         f"({p['wall_s']}s)")
            for lab, ok in p["steps"]:
                if not ok:
                    lines.append(f"  - ✗ {lab}")
        lines.append("")
    lines.append("_Billed cost is per-model actual usage priced from "
                 "`src/ctx/data/model-prices.json`; the Claude arms use the CLI's own "
                 "reported `total_cost_usd` where available._")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="triage")
    ap.add_argument("--arm", action="append", choices=ARMS,
                    help="repeatable; default: solo + orchestrated")
    ap.add_argument("--builder-model", default="gemini-3.6-flash")
    ap.add_argument("--fix-rounds", type=int, default=1)
    ap.add_argument("--build-timeout", type=float, default=1200.0)
    ap.add_argument("--out", default="evals/_runs/iterative")
    ap.add_argument("--keep", action="store_true", help="keep the build dirs")
    ns = ap.parse_args()
    arms = ns.arm or ["solo", "orchestrated"]
    if H._CHROME is None:
        print("no chromium found under /opt/pw-browsers", file=sys.stderr)
        return 2
    if "cross" in arms and H._agy_python() is None:
        print("cross arm needs the Antigravity SDK venv (CTX_AGY_PYTHON)", file=sys.stderr)
        return 2

    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for arm in arms:
        results.append(run_arm(arm, ns.task, ns.fix_rounds, ns.build_timeout,
                               ns.builder_model, ns.keep))
        (out / "records.json").write_text(json.dumps(results, indent=2), "utf-8")
    report = render(results)
    (out / "report.md").write_text(report, "utf-8")
    print("\n" + report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
