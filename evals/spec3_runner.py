#!/usr/bin/env python3
"""Three-arm spec-driven coding benchmark: naive vs straitjacket vs headroom.

Design goals (per the eval brief):
- Tasks mimic SPEC-DRIVEN coding: the fixture contains SPEC.md; the agent
  implements it, writes its own tests, iterates until green. Completion is
  graded by HELD-OUT acceptance tests the agent never sees (injected and
  run by this harness after the session), plus an independent 0-10 review
  by a stronger model reading the finished workspace.
- Arms differ ONLY in the wrapper: `claude` (naive), `ctx wrap claude
  --proxy` (straitjacket), `headroom wrap claude` (headroom). Same model,
  same fixture, same prompt, same tools, same turn cap. Fixtures include
  ctx.toml + git for ALL arms so the tree shape is identical.
- Metrics per arm: turns, cost, cache hit rate (cache_read / (cache_read +
  cache_creation + uncached input)), model-reported duration AND harness
  wall-clock (end-to-end including any proxy startup — that overhead is
  real), held-out acceptance pass fraction, reviewer score.

Usage:
    python3 evals/spec3_runner.py --out /tmp/spec3 [--model haiku]
        [--tasks tokenbucket csvq] [--review-model claude-opus-4-8]
        [--skip-review]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import textwrap
import time

MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": None}
TOOLS = "Bash Read Grep Glob Edit Write"
MAX_TURNS = 32
SESSION_TIMEOUT = 1500
REVIEW_TIMEOUT = 900

TASK_PROMPT = (
    "Read SPEC.md in this directory and implement it exactly. Write your "
    "own pytest tests, run them, and iterate until everything is green. "
    "Do not run git commit. Finish with one line: DONE: <number of tests "
    "you wrote>."
)

SPECS = {
    "tokenbucket": textwrap.dedent("""\
        # SPEC: token-bucket rate limiter

        Create a package `ratelimit/` with `bucket.py` providing:

        class TokenBucket(capacity: int, refill_per_sec: float, clock)
          - `clock` is a zero-arg callable returning seconds as float
            (injected; never call time.time() yourself).
          - Starts full (tokens == capacity).
          - allow(n: int = 1) -> bool: refill first (see below), then if at
            least n tokens remain, consume n and return True, else consume
            nothing and return False.
          - Refill is continuous: tokens += elapsed_seconds *
            refill_per_sec since the last refill, capped at capacity.
            Fractional tokens accumulate (floats).
          - ValueError on construction if capacity <= 0 or
            refill_per_sec < 0; ValueError from allow() if n <= 0.

        Also provide `ratelimit/__init__.py` re-exporting TokenBucket.

        Write pytest tests under `tests/` covering at minimum: a burst of
        exactly `capacity` allowed then denial; refill over injected-clock
        time enabling later allows; tokens never exceeding capacity after
        long idle; fractional refill accumulation; every ValueError case.
        """),
    "csvq": textwrap.dedent("""\
        # SPEC: csvq — tiny CSV query CLI

        Create a package `csvq/` runnable as `python -m csvq FILE --where
        COL OP VALUE [--count]` using ONLY the standard library (csv, sys,
        argparse).

        - OP is one of: eq, ne, gt, lt, contains.
        - Comparison: if BOTH the cell and VALUE parse as float, compare
          numerically; otherwise compare as strings (contains = substring).
        - Output: matching rows as CSV INCLUDING the header row, to
          stdout, preserving field quoting via the csv module. With
          --count, print ONLY the number of matching rows.
        - A missing FILE or unknown COL prints one error line to stderr
          and exits with code 2. Success exits 0 (even with 0 matches).

        Write pytest tests under `tests/` covering at minimum: numeric vs
        string comparison, contains, quoted fields containing commas,
        --count, the exit-2 error cases, and zero-match success.
        """),
}

HOLDOUT = {
    "tokenbucket": textwrap.dedent("""\
        import pytest
        from ratelimit import TokenBucket


        class Clock:
            def __init__(self): self.t = 100.0
            def __call__(self): return self.t


        def test_burst_then_deny():
            c = Clock(); b = TokenBucket(3, 1.0, c)
            assert [b.allow(), b.allow(), b.allow(), b.allow()] == [True, True, True, False]


        def test_refill_enables_allow():
            c = Clock(); b = TokenBucket(2, 0.5, c)
            assert b.allow(2) is True and b.allow() is False
            c.t += 2.0  # 1.0 token refilled
            assert b.allow() is True and b.allow() is False


        def test_cap_after_long_idle():
            c = Clock(); b = TokenBucket(2, 100.0, c)
            assert b.allow(2)
            c.t += 3600
            assert b.allow(2) is True and b.allow() is False


        def test_fractional_accumulation():
            c = Clock(); b = TokenBucket(1, 0.25, c)
            assert b.allow()
            c.t += 2.0
            assert b.allow() is False  # only 0.5 tokens
            c.t += 2.0
            assert b.allow() is True


        def test_multi_consume_all_or_nothing():
            c = Clock(); b = TokenBucket(3, 0.0, c)
            assert b.allow(2) is True
            assert b.allow(2) is False
            assert b.allow(1) is True  # the failed allow(2) consumed nothing


        @pytest.mark.parametrize("cap,rate", [(0, 1.0), (-1, 1.0), (1, -0.1)])
        def test_ctor_validation(cap, rate):
            with pytest.raises(ValueError):
                TokenBucket(cap, rate, lambda: 0.0)


        def test_allow_n_validation():
            b = TokenBucket(1, 1.0, lambda: 0.0)
            with pytest.raises(ValueError):
                b.allow(0)
        """),
    "csvq": textwrap.dedent("""\
        import csv, io, subprocess, sys
        from pathlib import Path

        DATA = 'name,team,score\\n"Doe, Jane",red,7.5\\nRex,blue,12\\nAri,red,3\\n'


        def run(args, tmp):
            f = tmp / "d.csv"; f.write_text(DATA)
            return subprocess.run(
                [sys.executable, "-m", "csvq", str(f), *args],
                capture_output=True, text=True)


        def rows(out):
            return list(csv.reader(io.StringIO(out)))


        def test_numeric_gt(tmp_path):
            r = run(["--where", "score", "gt", "5"], tmp_path)
            assert r.returncode == 0
            body = rows(r.stdout)
            assert body[0] == ["name", "team", "score"]
            assert ["Doe, Jane", "red", "7.5"] in body and ["Rex", "blue", "12"] in body
            assert len(body) == 3


        def test_string_eq_and_quoting(tmp_path):
            r = run(["--where", "name", "eq", "Doe, Jane"], tmp_path)
            assert r.returncode == 0 and rows(r.stdout)[1][0] == "Doe, Jane"


        def test_contains(tmp_path):
            r = run(["--where", "name", "contains", "oe"], tmp_path)
            assert len(rows(r.stdout)) == 2


        def test_count_only(tmp_path):
            r = run(["--where", "team", "eq", "red", "--count"], tmp_path)
            assert r.returncode == 0 and r.stdout.strip() == "2"


        def test_zero_matches_ok(tmp_path):
            r = run(["--where", "team", "eq", "green", "--count"], tmp_path)
            assert r.returncode == 0 and r.stdout.strip() == "0"


        def test_unknown_column_exit2(tmp_path):
            r = run(["--where", "nope", "eq", "x"], tmp_path)
            assert r.returncode == 2 and r.stderr.strip()


        def test_missing_file_exit2(tmp_path):
            r = subprocess.run(
                [sys.executable, "-m", "csvq", str(tmp_path / "absent.csv"),
                 "--where", "a", "eq", "b"], capture_output=True, text=True)
            assert r.returncode == 2 and r.stderr.strip()
        """),
}

REVIEW_PROMPT = (
    "You are an exacting staff engineer reviewing a spec-driven coding "
    "task done by another engineer in this directory. Read SPEC.md, then "
    "the implementation and its tests. You may run the tests. Score the "
    "work 0-10: spec compliance and correctness (0-4), the engineer's own "
    "test quality/coverage (0-3), code quality/clarity (0-3). Be strict "
    "but fair; do not modify anything. Finish with EXACTLY one final "
    "line: SCORE: <integer 0-10> | <one-sentence justification>."
)


def make_fixture(task: str, dest: pathlib.Path) -> None:
    dest.mkdir(parents=True)
    (dest / "SPEC.md").write_text(SPECS[task], encoding="utf-8")
    (dest / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "spec"],
        cwd=dest, check=True,
    )


def arm_argv(arm: str, model: str) -> list[str]:
    base = ["claude", "-p", TASK_PROMPT, "--max-turns", str(MAX_TURNS),
            "--output-format", "json", "--allowedTools", TOOLS]
    if MODELS[model]:
        base += ["--model", MODELS[model]]
    if arm == "naive":
        return base
    if arm == "sj":
        return ["ctx", "wrap", "claude", "--proxy", "--"] + base[1:]
    if arm == "headroom":
        return ["headroom", "wrap", "claude", "--"] + base[1:]
    raise ValueError(arm)


def parse_result_json(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def run_arm(task: str, arm: str, model: str, out: pathlib.Path) -> dict:
    base = out / f"{task}_{arm}"
    if base.exists():
        shutil.rmtree(base)
    make_fixture(task, base)
    cfg = out / f"cc-{task}-{arm}"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg),
           "PIP_REQUIRE_VIRTUALENV": "1"}
    t0 = time.monotonic()
    proc = subprocess.run(
        arm_argv(arm, model), cwd=base, env=env,
        capture_output=True, text=True, timeout=SESSION_TIMEOUT,
    )
    wall = round(time.monotonic() - t0, 1)
    (out / f"{task}_{arm}.raw").write_text(proc.stdout, encoding="utf-8")
    (out / f"{task}_{arm}.err").write_text(proc.stderr, encoding="utf-8")
    doc = parse_result_json(proc.stdout)
    u = doc.get("usage", {})
    reads = u.get("cache_read_input_tokens") or 0
    writes = u.get("cache_creation_input_tokens") or 0
    uncached = u.get("input_tokens") or 0
    denom = reads + writes + uncached
    # Held-out acceptance: inject tests the agent never saw, run them.
    hold = base / "tests_holdout"
    hold.mkdir(exist_ok=True)
    (hold / f"test_holdout_{task}.py").write_text(HOLDOUT[task], encoding="utf-8")
    acc = subprocess.run(
        ["python3", "-m", "pytest", "tests_holdout", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=base, capture_output=True, text=True, timeout=180,
    )
    m = re.search(r"(\d+) passed", acc.stdout)
    f = re.search(r"(\d+) failed", acc.stdout)
    e = re.search(r"(\d+) error", acc.stdout)
    passed = int(m.group(1)) if m else 0
    failed = (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0)
    total = passed + failed
    if total == 0:
        total = HOLDOUT[task].count("def test_")
    (out / f"{task}_{arm}.holdout").write_text(acc.stdout + acc.stderr, encoding="utf-8")
    return {
        "task": task, "arm": arm,
        "turns": doc.get("num_turns"),
        "cost_usd": round(doc.get("total_cost_usd") or 0.0, 4),
        "api_duration_s": round((doc.get("duration_ms") or 0) / 1000, 1),
        "wall_s": wall,
        "cache_hit_pct": round(100 * reads / denom, 1) if denom else None,
        "cache_read": reads, "cache_write": writes, "uncached_in": uncached,
        "output_tokens": u.get("output_tokens"),
        "holdout": f"{passed}/{total}",
        "holdout_frac": round(passed / total, 3) if total else 0.0,
        "session_error": (doc == {}),
    }


def review_arm(task: str, arm: str, review_model: str, out: pathlib.Path) -> dict:
    base = out / f"{task}_{arm}"
    # The reviewer must not see the held-out tests (they grade completion,
    # not quality) nor be told which arm produced the work.
    hold = base / "tests_holdout"
    if hold.exists():
        shutil.rmtree(hold)
    cfg = out / f"cc-review-{task}-{arm}"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg),
           "PIP_REQUIRE_VIRTUALENV": "1"}
    proc = subprocess.run(
        ["claude", "-p", REVIEW_PROMPT, "--max-turns", "14",
         "--output-format", "json", "--allowedTools", "Bash Read Grep Glob",
         "--model", review_model],
        cwd=base, env=env, capture_output=True, text=True, timeout=REVIEW_TIMEOUT,
    )
    (out / f"{task}_{arm}.review.raw").write_text(proc.stdout, encoding="utf-8")
    doc = parse_result_json(proc.stdout)
    result = doc.get("result") or ""
    m = re.search(r"SCORE:\s*(\d+)\s*(?:/10)?\s*\|\s*(.+)", result)
    return {
        "score": int(m.group(1)) if m else None,
        "justification": (m.group(2).strip() if m else result[-200:]),
        "review_cost_usd": round(doc.get("total_cost_usd") or 0.0, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="haiku", choices=list(MODELS))
    ap.add_argument("--tasks", nargs="+", default=list(SPECS), choices=list(SPECS))
    ap.add_argument("--arms", nargs="+", default=["naive", "sj", "headroom"])
    ap.add_argument("--review-model", default="claude-opus-4-8")
    ap.add_argument("--skip-review", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for task in args.tasks:
        # Arms run in parallel per task (isolated fixtures + config dirs).
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(run_arm, task, arm, args.model, args.out): arm
                    for arm in args.arms}
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result()
                rows.append(r)
                print(f"session done: {r['task']}/{r['arm']} · turns={r['turns']} "
                      f"cost=${r['cost_usd']} holdout={r['holdout']}", flush=True)
    if not args.skip_review:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(review_arm, r["task"], r["arm"],
                                args.review_model, args.out): r for r in rows}
            for fut in concurrent.futures.as_completed(futs):
                r = futs[fut]
                r.update(fut.result())
                print(f"review done: {r['task']}/{r['arm']} · score={r.get('score')}",
                      flush=True)

    rows.sort(key=lambda r: (r["task"], args.arms.index(r["arm"])))
    (args.out / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    cols = ["task", "arm", "turns", "cost_usd", "cache_hit_pct", "wall_s",
            "holdout", "score"]
    print("\n| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    print("\nSPEC3_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
