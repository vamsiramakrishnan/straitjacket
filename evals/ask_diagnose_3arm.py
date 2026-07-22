#!/usr/bin/env python3
"""Three-arm regression-diagnosis benchmark: naive vs Headroom vs straitjacket.

The task is the one ``ctx ask --intent diagnose`` / ``ctx q 'fails last |
in-changed'`` exist to collapse: a seeded regression (a raise inside ONE
changed function) fails the suite; the agent must name the culprit and make
the suite green. The gold culprit is known, so grading is model-free:

  success = (final message names the correct file:function)
            AND (pytest is green in the fixture workdir afterwards)

Arms (identical task, model, turn cap; isolated CLAUDE_CONFIG_DIR each):
  naive     plain ``claude -p``
  headroom  ``headroom wrap claude`` (proxy + tokensave/serena stack)
  sj        ``ctx wrap claude --proxy`` (the straitjacket skill + verbs)

Metrics come from ``--output-format json`` (num_turns, token classes,
cost) plus wall clock; the sj arm additionally reports whether the agent
actually invoked the shipped vocabulary (ctx ask/q/investigate) — the
question the skill update raises: does teaching the verbs change behavior?

Usage:
  python3 evals/ask_diagnose_3arm.py --out <dir> [--model haiku|sonnet]
          [--max-turns 18] [--arms naive headroom sj] [--repeats 1]

Determinism caveat (declared, like overhaul-3arm): live agents are
non-deterministic; this is N=repeats per arm with the seed fixed and every
raw result JSON kept next to the report. Not a resolve-rate claim.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": None}

GOLD_FILE = "orders/pricing.py"
GOLD_FUNC = "apply_discount"


def _pytest_python() -> str:
    """A pytest-capable interpreter for grading. The eval may be launched
    with a python that lacks pytest (the exact host-interpreter trap the
    profile-detection fix addressed); grade with one that has it, or the
    green/red signal is meaningless. Override with CTX_EVAL_PYTEST_PY."""
    env = os.environ.get("CTX_EVAL_PYTEST_PY")
    cands = [env] if env else []
    cands += [sys.executable]
    for base in ("python3", "python"):
        w = shutil.which(base)
        if w:
            cands.append(w)
    for py in cands:
        if not py:
            continue
        try:
            r = subprocess.run([py, "-c", "import pytest"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return py
        except (OSError, subprocess.SubprocessError):
            continue
    return sys.executable  # last resort; grading may be unreliable, declared


PYTEST_PY = _pytest_python()

# Compact verb card surfaced to the agent (sj_skill arm only) via CLAUDE.md —
# the vocabulary the Claude Code wrap path does NOT otherwise deliver. This
# is the skill's marginal-effect probe: same containment, plus the verbs.
VERB_CARD = """\
# Repository harness (ctx)

This repo is harnessed by `ctx`. For repository questions, prefer these
bounded verbs over a search/read/search loop:

- `ctx ask "<question>" --intent diagnose` — what explains the captured
  test failures. Reads the last captured run's failure facts and joins
  them against the change set; names the culprit symbol. It never reruns
  tests. (`--intent locate` = where is X; `--intent impact` = what breaks
  if X changes.)
- `ctx q 'fails last | in-changed'` — failing tests inside symbols changed
  this generation (the root-cause one-liner).
- `ctx run -- <cmd>` captures noisy output as a bounded digest + handle;
  `ctx get <handle>` pulls exact bytes.

Capture the failing suite once with `ctx run -- python -m pytest`, then
ask `ctx ask ... --intent diagnose`.
"""

TASK = (
    "The test suite in this repository is failing. Find the single "
    "root-cause function responsible and fix it so the whole suite passes "
    "again. Do not weaken or delete tests. When you are done, end your final "
    "message with one line in exactly this format:\n"
    "CULPRIT: <path/to/file.py>:<function_name>\n"
    "naming the function that contained the bug."
)

# ------------------------------------------------------------------ fixture
_PRICING = '''\
"""Order pricing: line totals, discounts, tax."""


def line_total(qty, unit_price):
    return qty * unit_price


def subtotal(lines):
    return sum(line_total(q, p) for q, p in lines)


def apply_discount(amount, pct):
    """Apply a percentage discount. pct is 0..100."""
    if pct < 0 or pct > 100:
        raise ValueError("pct out of range")
    return amount * (1 - pct / 100.0)


def apply_tax(amount, rate):
    return amount * (1 + rate)


def order_total(lines, discount_pct, tax_rate):
    return apply_tax(apply_discount(subtotal(lines), discount_pct), tax_rate)
'''

_PRICING_BUG = _PRICING.replace(
    "    return amount * (1 - pct / 100.0)",
    # The regression: divides by the wrong base — off by 10x. Deepest frame
    # of the failing assertions lands here, in this changed function.
    "    return amount * (1 - pct / 10.0)",
)

_TEST = '''\
from orders.pricing import (
    apply_discount, apply_tax, line_total, order_total, subtotal,
)


def test_line_total():
    assert line_total(3, 4) == 12


def test_subtotal():
    assert subtotal([(2, 5), (1, 10)]) == 20


def test_apply_discount():
    assert apply_discount(100, 10) == 90.0


def test_apply_tax():
    assert apply_tax(100, 0.2) == 120.0


def test_order_total():
    # Exact arithmetic (80 * 1.25 == 100.0) — no float-precision noise, so
    # the ONLY failing cause is the seeded apply_discount bug.
    assert order_total([(2, 50)], 20, 0.25) == 100.0
'''

_NOISE = "\n\n".join(
    f"def helper_{i:02d}(x):\n    # unrelated bookkeeping\n    return x + {i}"
    for i in range(24)
)


def build_fixture(root: pathlib.Path, verb_card: bool = False) -> None:
    if root.exists():
        shutil.rmtree(root)
    (root / "orders").mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.ctx*/\n",
                                     encoding="utf-8")
    (root / "orders" / "__init__.py").write_text("", encoding="utf-8")
    (root / "orders" / "pricing.py").write_text(_PRICING, encoding="utf-8")
    (root / "orders" / "util.py").write_text(_NOISE + "\n", encoding="utf-8")
    (root / "test_pricing.py").write_text(_TEST, encoding="utf-8")
    if verb_card:  # sj_skill arm: surface the vocabulary the wrap path omits
        (root / "CLAUDE.md").write_text(VERB_CARD, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "green baseline"], cwd=root,
                   check=True, env=env)
    # The regression, uncommitted: this generation's change set is exactly
    # pricing.py, so repo.changed / the root-cause join have their shape.
    (root / "orders" / "pricing.py").write_text(_PRICING_BUG, encoding="utf-8")


# --------------------------------------------------------------------- arms
# sj and sj_skill both wrap in ctx; sj_skill additionally surfaces the verb
# card (see build_fixture) — the only difference between them.
WRAPPED = {"sj", "sj_skill"}


def arm_argv(arm: str, model: str, max_turns: int) -> list[str]:
    base = ["claude", "-p", TASK, "--max-turns", str(max_turns),
            "--output-format", "json",
            "--allowedTools", "Bash Read Grep Glob Edit Write MultiEdit"]
    if MODELS[model]:
        base += ["--model", MODELS[model]]
    if arm == "naive":
        return base
    if arm in WRAPPED:
        return ["ctx", "wrap", "claude", "--proxy", "--"] + base[1:]
    if arm == "headroom":
        return ["headroom", "wrap", "claude", "--"] + base[1:]
    raise SystemExit(f"unknown arm {arm}")


def suite_green(workdir: pathlib.Path) -> bool:
    r = subprocess.run([PYTEST_PY, "-m", "pytest", "-q"],
                       cwd=workdir, capture_output=True, text=True, timeout=180)
    return r.returncode == 0


def grade(result_text: str, workdir: pathlib.Path) -> dict:
    m = re.search(r"CULPRIT:\s*([^\s:]+):(\w+)", result_text or "")
    named_file = named_func = None
    culprit_ok = False
    if m:
        named_file, named_func = m.group(1), m.group(2)
        file_ok = GOLD_FILE.endswith(named_file.lstrip("./")) or \
            named_file.endswith("pricing.py")
        culprit_ok = named_func == GOLD_FUNC and file_ok
    green = suite_green(workdir)
    return {
        "culprit_named": f"{named_file}:{named_func}" if m else None,
        "culprit_ok": bool(culprit_ok),
        # Primary success axis: did the agent actually fix the suite? Model-
        # free and robust to whether it emitted the summary tag (a turn-
        # capped agent may fix the code but never print CULPRIT:).
        "suite_green_after": green,
        "success": bool(green),
        "identified_culprit": bool(culprit_ok),
    }


def parse_result(raw: str) -> dict:
    """Pull the metrics from claude's result JSON (last JSON object on
    stdout — headroom prepends banner lines)."""
    for line in reversed(raw.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and '"type"' in line and "result" in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _usage_tokens(res: dict) -> dict:
    u = res.get("usage") or {}
    mu = res.get("modelUsage") or {}
    if not u and mu:  # headroom shape
        first = next(iter(mu.values()), {})
        u = {
            "input_tokens": first.get("inputTokens", 0),
            "output_tokens": first.get("outputTokens", 0),
            "cache_creation_input_tokens": first.get("cacheCreationInputTokens", 0),
            "cache_read_input_tokens": first.get("cacheReadInputTokens", 0),
        }
    return {
        "in": int(u.get("input_tokens", 0)),
        "out": int(u.get("output_tokens", 0)),
        "cache_create": int(u.get("cache_creation_input_tokens", 0)),
        "cache_read": int(u.get("cache_read_input_tokens", 0)),
    }


def used_ctx_vocab(cfg_dir: pathlib.Path) -> dict:
    """Did the wrapped agent actually invoke the shipped vocabulary? Count
    ``ctx <verb>`` occurrences in the agent's own tool-call commands,
    recorded in the Claude Code transcript under CLAUDE_CONFIG_DIR/projects
    (NOT the hook's own ``ctx hook …`` self-invocations, which are filtered
    by requiring a verb from the set)."""
    verbs = {"ask": 0, "q": 0, "investigate": 0, "plan": 0, "run": 0,
             "get": 0, "search": 0}
    for tx in cfg_dir.glob("projects/**/*.jsonl"):
        try:
            for cmd in re.findall(r'"command":"((?:[^"\\]|\\.)*)"',
                                  tx.read_text(encoding="utf-8")):
                for v in verbs:
                    verbs[v] += len(re.findall(rf"\bctx {v}\b", cmd))
        except OSError:
            pass
    return verbs


def run_arm(arm: str, model: str, max_turns: int, out: pathlib.Path,
            rep: int) -> dict:
    tag = f"{arm}_{model}_r{rep}"
    workdir = out / f"fixture_{tag}"
    build_fixture(workdir, verb_card=(arm == "sj_skill"))
    cfg = out / f"cc-{tag}"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg),
           "PIP_REQUIRE_VIRTUALENV": "1"}
    argv = arm_argv(arm, model, max_turns)
    t0 = time.monotonic()
    proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True,
                          text=True, timeout=2400)
    wall = time.monotonic() - t0
    (out / f"{tag}.stdout").write_text(proc.stdout, encoding="utf-8")
    (out / f"{tag}.stderr").write_text(proc.stderr, encoding="utf-8")
    res = parse_result(proc.stdout)
    g = grade(res.get("result", ""), workdir)
    toks = _usage_tokens(res)
    row = {
        "arm": arm, "model": model, "rep": rep,
        "num_turns": res.get("num_turns"),
        "terminal": res.get("subtype"),  # 'success' | 'error_max_turns' | …
        "hit_turn_cap": res.get("subtype") == "error_max_turns",
        "cost_usd": res.get("total_cost_usd") or res.get("costUSD"),
        "wall_s": round(wall, 1),
        "tokens": toks,
        **g,
    }
    if arm in WRAPPED:
        row["ctx_vocab"] = used_ctx_vocab(cfg)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="haiku", choices=list(MODELS))
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--arms", nargs="+",
                    default=["naive", "headroom", "sj", "sj_skill"])
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows = []
    for rep in range(1, args.repeats + 1):
        for arm in args.arms:
            print(f"running {arm} (rep {rep})…", flush=True)
            try:
                row = run_arm(arm, args.model, args.max_turns, args.out, rep)
            except subprocess.TimeoutExpired:
                row = {"arm": arm, "rep": rep, "error": "timeout"}
            rows.append(row)
            print("  " + json.dumps(row), flush=True)

    (args.out / "results.json").write_text(
        json.dumps({"model": args.model, "max_turns": args.max_turns,
                    "gold": f"{GOLD_FILE}:{GOLD_FUNC}", "rows": rows}, indent=2),
        encoding="utf-8",
    )
    print("\nwrote " + str(args.out / "results.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
