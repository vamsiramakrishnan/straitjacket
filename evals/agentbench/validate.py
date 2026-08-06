#!/usr/bin/env python3
"""Prove the referee before spending on it.

An agent benchmark is only as trustworthy as its grader, and a grader that
cannot tell the gold patch from an empty diff will happily report whatever the
harness wants to hear -- which is precisely how the suite this work started from
published 80% on a benchmark it never executed.

So: for every fixture, drive the adapter's own grade() through four states with
no model involved at all.

    baseline   bug present, nothing done      -> must NOT resolve
    gold       the real fix applied           -> MUST resolve
    tampered   fix applied but tests edited   -> must NOT resolve
    vandal     source replaced with garbage   -> must NOT resolve

`gold` failing means the grader is too strict and would score real fixes as
misses. `baseline` passing means it is too loose and everything resolves.
`tampered` passing means an agent can win by editing the tests. `vandal` passing
means the grader is not running anything.

Model-free, deterministic, seconds to run. Zero API cost.

Usage:
    python evals/agentbench/validate.py --adapter canary
"""
from __future__ import annotations

import argparse
import importlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent


def _vandalise(workdir: pathlib.Path) -> None:
    for path in (workdir / "pkg").glob("*.py"):
        if path.name != "__init__.py":
            path.write_text("raise RuntimeError('vandalised')\n", encoding="utf-8")


def _tamper(workdir: pathlib.Path) -> None:
    for path in (workdir / "tests").glob("test_*.py"):
        path.write_text("def test_trivially_true():\n    assert True\n", encoding="utf-8")


def check(adapter, task: dict, state: str, tmp: pathlib.Path) -> tuple[bool, dict]:
    workdir = tmp / f"{task['id'].replace('/', '_')}_{state}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    adapter.prepare(task, workdir)

    if state in ("gold", "tampered"):
        adapter.apply_gold(task, workdir)
    if state == "tampered":
        _tamper(workdir)
    if state == "vandal":
        _vandalise(workdir)

    result = adapter.grade(task, workdir)
    expected = state == "gold"
    return result.get("resolved") is expected, result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="canary")
    ap.add_argument("--n", type=int, default=99)
    args = ap.parse_args()

    sys.path.insert(0, str(HERE))
    adapter = importlib.import_module(f"adapters.{args.adapter}")
    if not hasattr(adapter, "apply_gold"):
        raise SystemExit(f"adapter '{args.adapter}' has no apply_gold(); cannot self-validate")

    tasks = adapter.load(args.n)
    states = ["baseline", "gold", "tampered", "vandal"]
    failures = 0

    with tempfile.TemporaryDirectory(prefix="agentbench_val_") as td:
        tmp = pathlib.Path(td)
        for task in tasks:
            for state in states:
                ok, result = check(adapter, task, state, tmp)
                want = "resolve" if state == "gold" else "NOT resolve"
                mark = "ok  " if ok else "FAIL"
                if not ok:
                    failures += 1
                print(
                    f"  {mark} {task['id']:18s} {state:9s} must {want:11s} "
                    f"-> resolved={str(result.get('resolved')):5s} "
                    f"f2p={result.get('f2p')} p2p={result.get('p2p')} "
                    f"tampered={result.get('tests_tampered')}",
                    flush=True,
                )

    total = len(tasks) * len(states)
    print(f"\nreferee controls: {total - failures}/{total} passed")
    if failures:
        print("REFEREE IS NOT SOUND -- do not run paid arms against it")
        return 1
    print("referee is sound: gold resolves, nothing else does")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
