#!/usr/bin/env python3
"""Coding-task suite A/B: naive vs straitjacket across many tasks × repeats.

The single-task eval (antigravity_sdk_eval.py) is one anecdote. This runs a
*registry* of diverse, self-contained coding tasks — each with a deterministic
verifier — through the same two-arm agent harness (Google Antigravity SDK,
gemini-3.5-flash), and repeats every task×arm so **success is a rate, not a
coin flip**, and token/turn numbers are medians over runs.

Arms differ in exactly one variable (reused verbatim from antigravity_sdk_eval):
  naive : the `shell` tool returns raw stdout+stderr (floods the transcript).
  sj    : `shell` routes through `ctx run` (bounded digest) + a `ctx_query`
          retrieval tool.

Task kinds span the honest spread — containment-favourable (quiet flood, big
failing suite, deep traceback), adversarial (greppable flood, where a
shell-savvy agent can skip the flood), and neutral (cross-file navigation, no
flood). The aggregate is therefore not cherry-picked.

Every task ships `tests/verify.py`, exit 0 ⇔ solved — one uniform, deterministic
grader across the suite.

Usage (from the SDK venv, ctx on PATH):
    python evals/coding_suite.py --repeats 2 --out evals/_runs/suite
    python evals/coding_suite.py --tasks quiet_flood,bigtest --repeats 3 --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# Reuse the proven single-task machinery + SDK symbols verbatim.
import antigravity_sdk_eval as base  # noqa: E402
from antigravity_sdk_eval import (  # noqa: E402
    Agent, BuiltinTools, CapabilitiesConfig, LocalAgentConfig, Meter,
    SYSTEM, hooks, make_tools, os, policy,
)

MODEL_DEFAULT = "gemini-3.5-flash"
WALL_TIMEOUT = 240.0


# ---------------------------------------------------------------- fixtures
def _flood(lines: int, needle: str, needle_at: int) -> str:
    rng = random.Random(20260720)
    workers = [f"w{i}" for i in range(1, 20)]
    out = []
    for i in range(1, lines + 1):
        out.append(needle if i == needle_at
                   else f"INFO {rng.choice(workers)} req-{i:05d} handled in {rng.randint(2,90)}ms")
    return "\n".join(out) + "\n"


def build_quiet_flood(root: pathlib.Path) -> None:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "router.py").write_text(
        "def route(region):\n"
        "    table = {'us': 'primary-us', 'eu': 'primary-eu', 'apac': 'primary-apac'}\n"
        "    return table.get(region, 'primary-' + region)\n", encoding="utf-8")
    needle = ("INFO health apac primary circuit OPEN for 6h; traffic must route "
              "via fallback-apac until restored")
    (root / "diagnose.py").write_text(
        "N=3000\nNEEDLE_AT=1637\n"
        "import random\nrng=random.Random(20260720)\n"
        "ws=[f'w{i}' for i in range(1,20)]\n"
        "for i in range(1,N+1):\n"
        "    if i==NEEDLE_AT:\n"
        f"        print({needle!r})\n"
        "    else:\n"
        "        print(f'INFO {rng.choice(ws)} req-{i:05d} handled in {rng.randint(2,90)}ms')\n",
        encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "verify.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
        "from app.router import route\n"
        "assert route('us')=='primary-us', route('us')\n"
        "assert route('apac')=='fallback-apac', route('apac')\n"
        "print('OK')\n", encoding="utf-8")


def build_keyword_flood(root: pathlib.Path) -> None:
    build_quiet_flood(root)  # same shape…
    # …but the needle is greppable (a shell-savvy agent can skip the flood).
    (root / "diagnose.py").write_text(
        "N=3000\nNEEDLE_AT=1637\n"
        "import random\nrng=random.Random(20260720)\n"
        "ws=[f'w{i}' for i in range(1,20)]\n"
        "for i in range(1,N+1):\n"
        "    if i==NEEDLE_AT:\n"
        "        print('FIXME apac primary down -> route apac via fallback-apac')\n"
        "    else:\n"
        "        print(f'INFO {rng.choice(ws)} req-{i:05d} handled in {rng.randint(2,90)}ms')\n",
        encoding="utf-8")


def build_traceback(root: pathlib.Path) -> None:
    (root / "pipeline.py").write_text(
        "def normalize(cfg):\n"
        "    # BUG: crashes when 'ratio' is absent; should default to 1.0\n"
        "    return cfg['ratio'] * 2\n"
        "def load(cfg):\n"
        "    return normalize(cfg)\n", encoding="utf-8")
    (root / "run.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))\n"
        "from pipeline import load\n"
        "for i in range(200):\n"
        "    print(f'DEBUG step {i} initialising subsystem {i%7}')\n"
        "print('DEBUG applying config {} (no ratio key)')\n"
        "print(load({'name': 'prod'}))\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "verify.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
        "from pipeline import normalize\n"
        "assert normalize({'name':'x'})==2.0, normalize({'name':'x'})\n"
        "assert normalize({'ratio':3})==6, normalize({'ratio':3})\n"
        "print('OK')\n", encoding="utf-8")


def build_bigtest(root: pathlib.Path) -> None:
    (root / "mathlib.py").write_text(
        "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n"
        "def is_even(n):\n    return n % 2 == 1   # BUG: inverted\n"
        "def last(xs):\n    return xs[len(xs)]     # BUG: off-by-one\n"
        "def merge(a, b):\n    d = dict(a); d.update(b); return d\n"
        "def sign(n):\n    return 1 if n > 0 else -1  # BUG: 0 should be 0\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    checks = []
    for i in range(0, 12):
        checks.append(f"chk('clamp {i}', clamp({i}, 2, 9)=={max(2, min(9, i))})")
    checks += [
        "chk('is_even 4', is_even(4)==True)", "chk('is_even 7', is_even(7)==False)",
        "chk('is_even 0', is_even(0)==True)",
        "chk('last', last([3,7,9])==9)", "chk('last2', last([1])==1)",
        "chk('merge', merge({'a':1},{'b':2})=={'a':1,'b':2})",
        "chk('sign+', sign(5)==1)", "chk('sign-', sign(-5)==-1)", "chk('sign0', sign(0)==0)",
    ]
    body = "\n".join("    " + c for c in checks)
    (root / "tests" / "verify.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
        "from mathlib import clamp, is_even, last, merge, sign\n"
        "fails=[]\n"
        "def chk(name, ok):\n    print(('PASS ' if ok else 'FAIL ')+name);\n"
        "    (fails.append(name) if not ok else None)\n"
        "def main():\n" + body + "\n"
        "    print(f'{len(fails)} failures')\n"
        "    sys.exit(1 if fails else 0)\n"
        "main()\n", encoding="utf-8")


def build_multifile(root: pathlib.Path) -> None:
    (root / "textutil.py").write_text(
        "def slugify(s):\n"
        "    # BUG: must also lowercase\n"
        "    return s.strip().replace(' ', '-')\n", encoding="utf-8")
    (root / "api.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))\n"
        "from textutil import slugify\n"
        "def make_slug(title):\n    return slugify(title)\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "verify.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
        "from api import make_slug\n"
        "assert make_slug(' Hello World ')=='hello-world', make_slug(' Hello World ')\n"
        "assert make_slug('AB Cd')=='ab-cd', make_slug('AB Cd')\n"
        "print('OK')\n", encoding="utf-8")


@dataclass
class Task:
    name: str
    kind: str
    build: Callable[[pathlib.Path], None]
    prompt: str


_FIX = ("Fix the project so `python3 tests/verify.py` exits 0 (prints OK / no failures).\n"
        "Use the `shell` tool to run commands. Iterate: reproduce, read the evidence, "
        "edit the source, re-run tests/verify.py until it passes. Then briefly say what you fixed.")

REGISTRY = [
    Task("quiet_flood", "flood-quiet", build_quiet_flood,
         "A test fails. First run `python3 diagnose.py` — its output explains how the "
         "'apac' region must now be routed (the reason is a single line in a large log). "
         "Then fix app/router.py accordingly.\n" + _FIX),
    Task("keyword_flood", "flood-keyword", build_keyword_flood,
         "A test fails. Run `python3 diagnose.py`; one line marked FIXME explains how "
         "'apac' must be routed. Fix app/router.py.\n" + _FIX),
    Task("traceback", "traceback", build_traceback,
         "`python3 run.py` crashes. Find the root cause in the traceback and fix "
         "pipeline.py so a missing 'ratio' key defaults sensibly.\n" + _FIX),
    Task("bigtest", "bigtest", build_bigtest,
         "The test suite `python3 tests/verify.py` reports several failures across "
         "mathlib.py. Fix every failing function.\n" + _FIX),
    Task("multifile", "multifile", build_multifile,
         "`python3 tests/verify.py` fails. The bug is in the helper that api.py calls, "
         "which lives in another file. Find it and fix it.\n" + _FIX),
]
BY_NAME = {t.name: t for t in REGISTRY}


# ---------------------------------------------------------------- runner
async def run_task_arm(task: Task, root: pathlib.Path, arm: str, model: str) -> dict:
    meter = Meter()
    root = root.resolve()
    cfg = LocalAgentConfig(
        model=model, api_key=os.environ["GEMINI_API_KEY"], workspaces=[str(root)],
        tools=make_tools(arm, root, meter),
        capabilities=CapabilitiesConfig(disabled_tools=[BuiltinTools.RUN_COMMAND]),
        hooks=[meter.hook(), meter.compaction_hook()],
        policies=[policy.allow_all()], system_instructions=SYSTEM,
        app_data_dir=str(root / ".agdata"),
    )
    t0 = time.monotonic()
    final_text, timed_out = "", False
    async with Agent(cfg) as agent:
        try:
            resp = await asyncio.wait_for(agent.chat(task.prompt), timeout=WALL_TIMEOUT)
            final_text = await resp.text()
        except asyncio.TimeoutError:
            timed_out = True
        usage = agent.conversation.total_usage
    correct = subprocess.run(["python3", "tests/verify.py"], cwd=str(root),
                             capture_output=True, text=True).returncode == 0
    return {
        "task": task.name, "kind": task.kind, "arm": arm,
        "correct": correct, "completed": bool(final_text) and not timed_out,
        "timed_out": timed_out,
        "billed_total": usage.total_token_count or 0,
        "billed_input": usage.prompt_token_count or 0,
        "tool_output_into_context": meter.tool_output_tokens,
        "raw_command_bytes": meter.raw_command_bytes,
        "tool_calls": meter.tool_calls, "compactions": meter.compactions,
        "wall_s": round(time.monotonic() - t0, 1),
    }


async def run_suite(tasks: list[Task], arms: list[str], repeats: int, model: str,
                    outdir: pathlib.Path | None) -> list[dict]:
    records: list[dict] = []
    for task in tasks:
        for rep in range(repeats):
            for arm in arms:
                with tempfile.TemporaryDirectory() as td:
                    root = pathlib.Path(td)
                    task.build(root)
                    print(f"[run] task={task.name} arm={arm} rep={rep+1}/{repeats}", flush=True)
                    try:
                        rec = await run_task_arm(task, root, arm, model)
                    except Exception as e:  # a single run failing must not abort the suite
                        rec = {"task": task.name, "kind": task.kind, "arm": arm,
                               "correct": False, "error": str(e)[:200], "wall_s": 0}
                    rec["rep"] = rep
                    records.append(rec)
                    if outdir:
                        outdir.mkdir(parents=True, exist_ok=True)
                        (outdir / f"{task.name}.{arm}.{rep}.json").write_text(
                            json.dumps(rec, indent=2), encoding="utf-8")
    return records


# ---------------------------------------------------------------- report
def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else 0


def aggregate(records: list[dict], arms: list[str]) -> dict:
    agg = {}
    for arm in arms:
        rs = [r for r in records if r["arm"] == arm]
        ok = [r for r in rs if r.get("correct")]
        agg[arm] = {
            "runs": len(rs),
            "success_rate": round(len(ok) / len(rs), 3) if rs else 0.0,
            "median_billed_total": _median([r.get("billed_total") for r in rs]),
            "median_tool_output_ctx": _median([r.get("tool_output_into_context") for r in rs]),
            "median_tool_calls": _median([r.get("tool_calls") for r in rs]),
            "median_wall_s": _median([r.get("wall_s") for r in rs]),
        }
    return agg


def render(records: list[dict], arms: list[str], model: str, repeats: int) -> str:
    agg = aggregate(records, arms)
    lines = [f"# Coding-task suite A/B — naive vs sj ({model})",
             f"{len(records)} runs · {len({r['task'] for r in records})} tasks × {repeats} repeats × {len(arms)} arms\n",
             "## Aggregate", "", "| metric | naive | sj |", "|---|---|---|"]
    def row(label, key, fmt="{:,}"):
        n = agg.get("naive", {}).get(key, 0); s = agg.get("sj", {}).get(key, 0)
        return f"| {label} | {fmt.format(n)} | {fmt.format(s)} |"
    lines += [
        row("success rate", "success_rate", "{:.0%}"),
        row("median billed tokens", "median_billed_total"),
        row("median tool-output into context", "median_tool_output_ctx"),
        row("median tool calls", "median_tool_calls", "{:.0f}"),
        row("median wall s", "median_wall_s", "{:.0f}"),
    ]
    lines += ["", "## Per task (success rate · median billed tok)", "",
              "| task | kind | naive | sj |", "|---|---|---|---|"]
    for name in [t.name for t in REGISTRY if any(r["task"] == t.name for r in records)]:
        rr = [r for r in records if r["task"] == name]
        def cell(arm):
            a = [r for r in rr if r["arm"] == arm]
            ok = sum(1 for r in a if r.get("correct"))
            return f"{ok}/{len(a)} · {_median([r.get('billed_total') for r in a]):,}" if a else "—"
        kind = rr[0]["kind"]
        lines.append(f"| {name} | {kind} | {cell('naive')} | {cell('sj')} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="", help="comma list; default = all")
    ap.add_argument("--arms", default="naive,sj")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    tasks = [BY_NAME[n] for n in args.tasks.split(",") if n] if args.tasks else list(REGISTRY)
    arms = [a for a in args.arms.split(",") if a]
    outdir = pathlib.Path(args.out) if args.out else None
    records = asyncio.run(run_suite(tasks, arms, args.repeats, args.model, outdir))
    if args.json:
        print(json.dumps({"model": args.model, "repeats": args.repeats,
                          "records": records, "aggregate": aggregate(records, arms)}, indent=2))
    else:
        print(render(records, arms, args.model, args.repeats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
