#!/usr/bin/env python3
"""Agent-harness referee: plain Claude versus the full wrapper bundle.

The tokenomics eval (`evals/tokenomics/`) drives a fixed model ladder -- a
script that calls an API, runs a subprocess, calls an API again. That measures a
digest formatter, because nothing in the loop can decide to run a command or
follow an address. This harness drives a REAL agent instead:

    naive :  claude -p "<task>" --max-turns N --allowedTools "..."
    sj    :  ctx wrap claude --proxy -- -p "<task>" --max-turns N ...

The task prompt, fixture, requested tool list, and turn cap match. The effective
intervention is broader than output containment: `ctx wrap` can inject guidance,
expose ctx tools, proxy traffic, and change native-tool availability. Model
parity is auditable only when `--model` is supplied; a null model field means
both commands used their host default, not that the resolved model was recorded.
Fixtures carry `ctx.toml` and git for both arms so the tree shape is identical.

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
import concurrent.futures
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent

# Kept byte-identical to spec3_runner's referee constants where they overlap.
TOOLS = "Bash Read Grep Glob Edit Write"
MAX_TURNS = 40
SESSION_TIMEOUT = 2400

ARMS = ("naive", "sj", "sj_rescue", "headroom", "maki")


def arm_argv(arm: str, prompt: str, model: str | None, max_turns: int,
             port: int | None = None) -> list[str]:
    """Build agent commands; the sj prefix activates the full wrapper bundle."""
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
    if arm == "sj_rescue":
        # The full wrapper plus the opt-in Tier-1 rescue: deterministic,
        # addressable elision of the transcript once the window passes the
        # threshold. Sessions here peak near 48% of a 200k window, so the
        # default engages from mid-session; AGENTBENCH_RESCUE_PCT overrides.
        pct = os.environ.get("AGENTBENCH_RESCUE_PCT", "25")
        return ["ctx", "wrap", "claude", "--proxy", "--rescue-pct", pct, "--"] + base[1:]
    if arm == "headroom":
        # headroom-ai (pip install "headroom-ai[proxy]"): a compression proxy
        # between Claude Code and the API, vendor defaults except the port,
        # which must be unique per concurrent session (their default is 8787
        # for every wrap). Binary overridable for a venv install.
        return [os.environ.get("AGENTBENCH_HEADROOM", "headroom"), "wrap", "claude",
                "--port", str(port or _free_port()), "--"] + base[1:]
    if arm == "maki":
        # maki.sh: a different agent, not a wrapper. Its --print mode is a
        # drop-in for Claude Code's (same JSON result fields), so the same
        # parser reads cost, usage and turns. Needs ANTHROPIC_API_KEY.
        return [os.environ.get("AGENTBENCH_MAKI", "maki"), prompt, "--print",
                "--output-format", "json", "--max-turns", str(max_turns),
                "--allowed-tools", ",".join(TOOLS.split()), "--yolo", "--trust",
                *(["--model", _maki_model(model)] if model else [])]
    raise ValueError(f"unknown arm: {arm}")


#: Claude Code resolves aliases like `haiku`; maki wants provider/model-id.
_MAKI_ALIASES = {
    "haiku": "anthropic/claude-haiku-4-5",
    "sonnet": "anthropic/claude-sonnet-5",
    "opus": "anthropic/claude-opus-5",
}


def _maki_model(model: str) -> str:
    if model in _MAKI_ALIASES:
        return _MAKI_ALIASES[model]
    return model if "/" in model else f"anthropic/{model}"


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prompt_prefix(cfg: pathlib.Path) -> dict | None:
    """First-request prompt shape of a Claude Code session (system bytes,
    tool count, tool-catalogue bytes, deferral), read from its transcript.
    This is the per-arm evidence for the prefix tax the DeepSWE receipt found;
    None for an arm that is not Claude Code (maki keeps no such transcript)."""
    try:
        from ctx.wrap import _prompt_snapshot
    except ImportError:
        return None
    snap = _prompt_snapshot(cfg)
    if not snap:
        return None
    return {k: v for k, v in snap.items() if k != "tool_names"}


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
    # The host resolves aliases like `haiku`; record what it actually billed
    # so model parity is auditable from the record, not from the flag.
    used = sorted((doc.get("modelUsage") or {}).keys())
    return {
        "model_used": ",".join(used) if used else None,
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
            max_turns: int, repeat: int, work_root: pathlib.Path) -> dict:
    """One (task, arm, repeat): materialize, run the agent, grade."""
    tag = f"{task['id']}_{arm}_r{repeat}".replace("/", "_")
    # Fixtures live OUTSIDE the repository under test. An agent whose cwd sits
    # inside straitjacket's own tree can walk up into it; keeping the sandbox
    # elsewhere makes that impossible rather than merely unlikely.
    workdir = (work_root / tag).resolve()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    prompt = adapter.prepare(task, workdir)

    # Isolated agent config per run, so one arm cannot warm another's state.
    # MUST be absolute: the child runs with cwd=workdir, so a relative
    # CLAUDE_CONFIG_DIR resolves against the FIXTURE and the agent's own config
    # tree materializes inside the workspace being graded.
    cfg = (out / "cfg" / tag).resolve()
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg), "PIP_REQUIRE_VIRTUALENV": "1",
           # maki keeps its config under XDG; isolate it the same way.
           "XDG_CONFIG_HOME": str(cfg / "xdg"), "HEADROOM_HOME": str(cfg / "headroom")}
    # An adapter that builds a per-run toolchain (a venv, an image's ENV) hands
    # it to the agent here. Both arms receive the identical mapping.
    if hasattr(adapter, "session_env"):
        env.update(adapter.session_env(task, workdir))

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
        "prefix": prompt_prefix(cfg),
    }
    # Grading is the adapter's job and never trusts the agent's own claims.
    rec.update(adapter.grade(task, workdir))
    return rec


def load_adapter(name: str):
    return importlib.import_module(f"adapters.{name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="canary | swebench | deepswe | dogfood")
    ap.add_argument("--arms", nargs="+", default=["naive", "sj"], choices=ARMS)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--model", default=None, help="passed to --model; default = host default")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "results")
    ap.add_argument("--work-root", type=pathlib.Path, default=None,
                    help="where fixtures are materialized; defaults to a temp dir "
                         "OUTSIDE this repo so an agent cannot reach the repo under test")
    ap.add_argument("--adapter-arg", action="append", default=[],
                    help="key=value passed through to the adapter's load()")
    ap.add_argument("--jobs", type=int, default=1,
                    help="concurrent (task, arm, repeat) sessions; each has its own "
                         "fixture, config dir and toolchain, so runs cannot share state")
    ap.add_argument("--label", default=None,
                    help="free-text tag stored in the payload and shown by report.py, "
                         "e.g. the wrapper version under test")
    ap.add_argument("--prefix-parity", action="store_true",
                    help="before any paid arm, run `ctx wrap claude --probe-prefix` (one naive "
                         "turn, one wrapped turn) and refuse to run when the wrapper adds more "
                         "prefix than it declares or loses tool deferral; the verdict is stored "
                         "in the payload (--allow-prefix-tax runs anyway and records the failure)")
    ap.add_argument("--allow-prefix-tax", action="store_true")
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

    # Absolute from here on: these paths are handed to a child whose cwd is the
    # fixture, where a relative path means something else entirely.
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    work_root = (args.work_root.resolve() if args.work_root
                 else pathlib.Path(tempfile.mkdtemp(prefix="agentbench_work_")))
    work_root.mkdir(parents=True, exist_ok=True)
    print(f"work root: {work_root}", flush=True)
    print(f"adapter={args.adapter} tasks={len(tasks)} arms={args.arms} repeats={args.repeats}",
          flush=True)

    if "maki" in args.arms and not os.environ.get("ANTHROPIC_API_KEY"):
        # maki is not Claude Code: it cannot use Claude Code's login, and a
        # keyless run returns a JSON error after zero turns. Refuse before
        # the other arms spend anything.
        raise SystemExit("the maki arm needs ANTHROPIC_API_KEY in the environment "
                         "(maki authenticates itself; Claude Code's login does not apply)")

    prefix_parity = None
    if args.prefix_parity and "sj" in args.arms:
        # The referee for the wrapper itself: a paid sweep is only worth
        # running when the wrapper's per-request cost is what it declares.
        from ctx.wrap import probe_prefix, render_prefix_parity

        prefix_parity = probe_prefix(work_root, model=args.model or "haiku")
        print(render_prefix_parity(prefix_parity), flush=True)
        if not prefix_parity.get("ok") and not args.allow_prefix_tax:
            raise SystemExit("prefix parity FAILED: the wrapper would tax every request; "
                             "fix it or pass --allow-prefix-tax to record the failure and run anyway")

    records: list[dict] = []
    lock = threading.Lock()

    def record(rec: dict) -> None:
        with lock:
            records.append(rec)
            print(
                f"  [{rec['arm']}] r{rec['repeat']} {rec['task_id']:44s} "
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
                "jobs": args.jobs,
                "label": args.label,
                "prefix_parity": prefix_parity,
                "task_ids": [t["id"] for t in tasks],
                "provenance": "live",
                "simulated": False,
                "work_root": str(work_root),
                "results": records,
            }
            # In-flight runs write to a .partial file: a results file
            # named like a finished one, holding half the arms, reads as a
            # complete eval to anything that globs the directory -- report.py
            # included. Renamed to the real name only once every arm lands.
            (args.out / f"{args.adapter}.partial.json").write_text(
                json.dumps(payload, indent=1), encoding="utf-8")

    def one(task: dict, arm: str, repeat: int) -> None:
        try:
            rec = run_one(adapter, task, arm, args.model, args.out,
                          args.max_turns, repeat, work_root)
        except Exception as exc:  # noqa: BLE001 - a broken fixture is a row, not a crash
            rec = {"task_id": task["id"], "arm": arm, "repeat": repeat, "provenance": "live",
                   "resolved": False, "session_error": True, "harness_error": repr(exc)[:500]}
        record(rec)

    jobs = [(task, arm, repeat)
            for repeat in range(1, args.repeats + 1)
            for task in tasks
            for arm in args.arms]
    if args.jobs <= 1:
        for job in jobs:
            one(*job)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(lambda j: one(*j), jobs))

    partial = args.out / f"{args.adapter}.partial.json"
    final = args.out / f"{args.adapter}.json"
    if partial.exists():
        partial.replace(final)
    print(f"-> {final}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
