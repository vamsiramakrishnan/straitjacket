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
import concurrent.futures
import contextlib
import importlib
import pathlib
import shutil
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


# The SWE-bench-shaped defaults. An adapter whose grader has different cheat
# surfaces (DeepSWE discards agent test edits by construction) declares its own
# CONTROL_STATES {state: must_resolve} and control(task, workdir, state).
DEFAULT_STATES = {"baseline": False, "gold": True, "tampered": False, "vandal": False}


def check(adapter, task: dict, state: str, expected: bool,
          tmp: pathlib.Path, keep: bool = False) -> tuple[bool, dict]:
    workdir = tmp / f"{task['id'].replace('/', '_')}_{state}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        adapter.prepare(task, workdir)
        if hasattr(adapter, "control"):
            adapter.control(task, workdir, state)
        else:
            if state in ("gold", "tampered"):
                adapter.apply_gold(task, workdir)
            if state == "tampered":
                _tamper(workdir)
            if state == "vandal":
                _vandalise(workdir)
        result = adapter.grade(task, workdir)
    except Exception as exc:  # noqa: BLE001 - an unbuildable fixture is a FAIL row, not a crash
        result = {"resolved": None, "error": repr(exc)[:300]}
    finally:
        # A checkout plus its toolchain can run to hundreds of MB, and a
        # sweep materializes tasks x states of them. Only a failed control
        # is worth keeping around for inspection.
        if not keep and result.get("resolved") is expected:
            for p in tmp.glob(f"{workdir.name}*"):
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    return result.get("resolved") is expected, result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="canary")
    ap.add_argument("--n", type=int, default=99)
    ap.add_argument("--adapter-arg", action="append", default=[],
                    help="key=value passed through to the adapter's load()")
    ap.add_argument("--jobs", type=int, default=1, help="concurrent (task, state) checks")
    ap.add_argument("--work-root", type=pathlib.Path, default=None,
                    help="keep fixtures here instead of a temp dir (inspect failures)")
    ap.add_argument("--states", nargs="+", default=None,
                    help="subset of control states to run (default: all)")
    ap.add_argument("--keep", action="store_true",
                    help="keep every fixture (default: only failed controls are kept)")
    args = ap.parse_args()

    sys.path.insert(0, str(HERE))
    adapter = importlib.import_module(f"adapters.{args.adapter}")
    if not hasattr(adapter, "apply_gold"):
        raise SystemExit(f"adapter '{args.adapter}' has no apply_gold(); cannot self-validate")

    kw = {}
    for item in args.adapter_arg:
        k, _, v = item.partition("=")
        kw[k] = v
    tasks = adapter.load(args.n, **kw)
    states = dict(getattr(adapter, "CONTROL_STATES", DEFAULT_STATES))
    if args.states:
        states = {s: states[s] for s in args.states}
    failures = 0

    if args.work_root:
        args.work_root.mkdir(parents=True, exist_ok=True)
        tmp_ctx = contextlib.nullcontext(str(args.work_root.resolve()))
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="agentbench_val_")

    with tmp_ctx as td:
        tmp = pathlib.Path(td)
        jobs = [(task, state, expected) for task in tasks for state, expected in states.items()]

        def one(job):
            task, state, expected = job
            ok, result = check(adapter, task, state, expected, tmp, keep=args.keep)
            want = "resolve" if expected else "NOT resolve"
            mark = "ok  " if ok else "FAIL"
            print(
                f"  {mark} {task['id']:44s} {state:9s} must {want:11s} "
                f"-> resolved={str(result.get('resolved')):5s} "
                f"f2p={result.get('f2p')} p2p={result.get('p2p')} "
                f"tampered={result.get('tests_tampered')}"
                + (f" error={result['error']}" if result.get("error") else ""),
                flush=True,
            )
            return ok

        if args.jobs > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                outcomes = list(pool.map(one, jobs))
        else:
            outcomes = [one(j) for j in jobs]
        failures = sum(1 for ok in outcomes if not ok)

    total = len(tasks) * len(states)
    print(f"\nreferee controls: {total - failures}/{total} passed")
    if failures:
        print("REFEREE IS NOT SOUND -- do not run paid arms against it")
        return 1
    print("referee is sound: gold resolves, nothing else does")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
