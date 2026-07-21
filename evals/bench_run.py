#!/usr/bin/env python3
"""straitjacket-bench runner: naive vs Headroom vs straitjacket over the
diverse scenario dataset (evals/bench/dataset.py).

Each (scenario, arm, rep) runs a real Claude Code agent in an isolated
fixture with the scenario's turn cap, then a model-free grader assigns
success. Arms (identical task/model/cap; isolated CLAUDE_CONFIG_DIR each):

  naive     plain `claude -p`
  headroom  `headroom wrap claude` (proxy + tokensave stack)
  sj        `ctx wrap claude --proxy` PLUS the ctx verb card in the
            fixture's CLAUDE.md — i.e. straitjacket as `install_claude`
            actually delivers it (hooks + proxy + the teaching surface).

Metrics from `--output-format json` (turns, token classes, cost) + wall
clock + the deterministic grade; the sj arm also reports ctx-vocab
adoption from the transcript.

Usage:
  python3 evals/bench_run.py --out DIR [--model haiku] [--repeats 1]
          [--arms naive headroom sj] [--only bug-flood,comp-grep]

Determinism caveat (declared): live agents are non-deterministic. This is
N=repeats per cell with fixed-seed fixtures; grading is deterministic and
every raw result JSON is kept. Not a resolve-rate claim about any model.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bench.dataset import BY_ID, SCENARIOS  # noqa: E402

MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": None}
WRAPPED = {"sj", "sj-collapse"}
TOOLS = "Bash Read Grep Glob Edit Write MultiEdit"

VERB_CARD = """\
# Repository harness (ctx / straitjacket)

This workspace is harnessed by `ctx`. Prefer these bounded verbs over a
search/read/search loop or paging large output:

- `ctx ask "<q>" --intent locate|impact|diagnose|trace` — one bounded
  evidence view. diagnose reads the last captured run's failure facts (it
  never reruns); locate = where is X; impact = what calls X; trace = the
  call path through X.
- `ctx q '<pipeline>'` — typed streams: `refs X | group file`,
  `fails last | in-changed`, `records <handle> --jsonl | group F | count`,
  `search P --glob G`. Bounded, no re-reading whole files.
- `ctx run -- <cmd>` captures noisy output (tests, logs) as a bounded
  digest + handle; `ctx get <handle>` pulls exact bytes. Cite handles.
"""


def _pytest_python() -> str:
    env = os.environ.get("CTX_EVAL_PYTEST_PY")
    for py in ([env] if env else []) + [sys.executable, "python3", "python"]:
        if not py:
            continue
        try:
            if subprocess.run([py, "-c", "import pytest"],
                              capture_output=True, timeout=20).returncode == 0:
                return py
        except (OSError, subprocess.SubprocessError):
            continue
    return sys.executable


PYTEST_PY = _pytest_python()


def arm_argv(arm, model, prompt, cap):
    # sj-collapse removes the native Grep/Glob tools so search is forced onto
    # the doors the replacement surface controls: Bash grep (→ substituted) or
    # ctx verbs (→ already collapsed). This is the wozcode move's missing half
    # — replace the surface means also *remove* the default tools.
    tools = "Bash Read Edit Write MultiEdit" if arm == "sj-collapse" else TOOLS
    base = ["claude", "-p", prompt, "--max-turns", str(cap),
            "--output-format", "json", "--allowedTools", tools]
    if MODELS[model]:
        base += ["--model", MODELS[model]]
    if arm == "naive":
        return base
    if arm in ("sj", "sj-collapse"):
        return ["ctx", "wrap", "claude", "--proxy", "--"] + base[1:]
    if arm == "headroom":
        return ["headroom", "wrap", "claude", "--"] + base[1:]
    raise SystemExit(f"unknown arm {arm}")


def parse_result(raw):
    for line in reversed((raw or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and '"type"' in line and "result" in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _tokens(res):
    u = res.get("usage") or {}
    mu = res.get("modelUsage") or {}
    if not u and mu:
        f = next(iter(mu.values()), {})
        u = {"input_tokens": f.get("inputTokens", 0),
             "output_tokens": f.get("outputTokens", 0),
             "cache_creation_input_tokens": f.get("cacheCreationInputTokens", 0),
             "cache_read_input_tokens": f.get("cacheReadInputTokens", 0)}
    return {"in": int(u.get("input_tokens", 0)), "out": int(u.get("output_tokens", 0)),
            "cache_create": int(u.get("cache_creation_input_tokens", 0)),
            "cache_read": int(u.get("cache_read_input_tokens", 0))}


def _vocab(cfg_dir):
    verbs = {v: 0 for v in ("ask", "q", "investigate", "run", "get", "search")}
    for tx in pathlib.Path(cfg_dir).glob("projects/**/*.jsonl"):
        try:
            for cmd in re.findall(r'"command":"((?:[^"\\]|\\.)*)"',
                                  tx.read_text(encoding="utf-8")):
                for v in verbs:
                    verbs[v] += len(re.findall(rf"\bctx {v}\b", cmd))
        except OSError:
            pass
    return verbs


def run_cell(scn, arm, model, rep, out):
    tag = f"{scn.id}__{arm}__r{rep}"
    workdir = out / f"fx_{tag}"
    if workdir.exists():
        import shutil
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    scn.build(workdir)
    if arm in ("sj", "sj-collapse"):  # deliver the teaching surface
        (workdir / "CLAUDE.md").write_text(VERB_CARD, encoding="utf-8")
    if arm == "sj-collapse":  # the replacement surface: opt into collapse
        (workdir / "ctx.toml").write_text(
            'version = 1\n[guard]\ncollapse = true\n', encoding="utf-8")
    cfg = out / f"cc_{tag}"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg), "PIP_REQUIRE_VIRTUALENV": "1"}
    argv = arm_argv(arm, model, scn.prompt, scn.turn_cap)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True,
                              text=True, timeout=1800)
        stdout, timed_out = proc.stdout, False
    except subprocess.TimeoutExpired as e:
        stdout, timed_out = (e.stdout or ""), True
    wall = time.monotonic() - t0
    (out / f"{tag}.stdout").write_text(stdout or "", encoding="utf-8")
    res = parse_result(stdout)
    final = res.get("result", "") or ""
    try:
        g = scn.grade(workdir, final, PYTEST_PY)
    except Exception as e:  # a grader must never crash the run
        g = {"success": False, "grade_error": str(e)}
    toks = _tokens(res)
    row = {
        "scenario": scn.id, "category": scn.category, "flood": scn.flood,
        "arm": arm, "model": model, "rep": rep,
        "success": bool(g.get("success")),
        "num_turns": res.get("num_turns"),
        "terminal": "timeout" if timed_out else res.get("subtype"),
        "cost_usd": res.get("total_cost_usd") or res.get("costUSD"),
        "wall_s": round(wall, 1), "tokens": toks, "grade": g,
    }
    if arm in ("sj", "sj-collapse"):
        row["ctx_vocab"] = _vocab(cfg)
    if arm == "sj-collapse":
        row["collapse_fires"] = _collapse_fires(workdir)
    return row


def _collapse_fires(workdir):
    """How many loop-shapes the replacement surface actually collapsed, by
    shape, from the fixture's collapse.jsonl ledger."""
    out = {}
    led = pathlib.Path(workdir) / ".ctx-session-reads" / "collapse.jsonl"
    try:
        for line in led.read_text(encoding="utf-8").splitlines():
            try:
                shape = json.loads(line).get("shape", "?")
            except json.JSONDecodeError:
                continue
            out[shape] = out.get(shape, 0) + 1
    except OSError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="haiku", choices=list(MODELS))
    ap.add_argument("--arms", nargs="+", default=["naive", "headroom", "sj"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    ids = [s.strip() for s in args.only.split(",") if s.strip()] or [s.id for s in SCENARIOS]
    scns = [BY_ID[i] for i in ids]
    rows = []
    for rep in range(1, args.repeats + 1):
        for scn in scns:
            for arm in args.arms:
                print(f"[{scn.id} · {arm} · r{rep}] running…", flush=True)
                row = run_cell(scn, arm, args.model, rep, args.out)
                rows.append(row)
                print(f"  success={row['success']} turns={row['num_turns']} "
                      f"cost={row['cost_usd']} wall={row['wall_s']}s", flush=True)
                # incremental persist (survives a mid-run kill)
                (args.out / "results.json").write_text(
                    json.dumps({"model": args.model, "rows": rows}, indent=2),
                    encoding="utf-8")
    print("\nwrote " + str(args.out / "results.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
