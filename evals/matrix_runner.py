#!/usr/bin/env python3
"""Scenario-matrix benchmark runner: {scenarios} x {models} x {naive, sj}.

Each pair runs both arms in parallel, each arm in a fresh fixture with an
isolated CLAUDE_CONFIG_DIR (lesson: isolate, don't serialize). Results land
as <out>/<scenario>_<model>_<arm>.json plus fixture dirs for grading.

Usage:
    python3 evals/matrix_runner.py --repo /path/to/straitjacket \
        --out /tmp/matrix --pairs S1:sonnet S1:haiku S2:sonnet ...
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import shutil
import subprocess
import textwrap

MODELS = {
    "sonnet": None,  # host default
    "haiku": "claude-haiku-4-5-20251001",
}

TOOLS = "Bash Read Grep Glob Edit Write MultiEdit"

SURGICAL_SRC = {
    "pager/__init__.py": "",
    "pager/paginate.py": textwrap.dedent('''\
        """Pagination bounds helper."""


        def page_bounds(total: int, page_size: int) -> list[tuple[int, int]]:
            """Return (start, end) index pairs covering ``total`` items.

            ``end`` is exclusive. The final page must be truncated to
            ``total``.
            """
            pages = []
            start = 0
            while start < total:
                end = start + page_size
                pages.append((start, end))
                start += page_size
            return pages
        '''),
    "tests/test_paginate.py": textwrap.dedent('''\
        from pager.paginate import page_bounds


        def test_even_split():
            assert page_bounds(10, 5) == [(0, 5), (5, 10)]


        def test_single_page():
            assert page_bounds(3, 10) == [(0, 3)]


        def test_last_page_truncated():
            assert page_bounds(7, 3) == [(0, 3), (3, 6), (6, 7)]


        def test_empty():
            assert page_bounds(0, 4) == []


        def test_page_size_one():
            assert page_bounds(2, 1) == [(0, 1), (1, 2)]
        '''),
}

TASKS = {
    "S1": (
        "The test suite in this repository fails. Run it, find the bug, and "
        "make all tests pass with a minimal fix. Do NOT modify any file under "
        "tests/. Do not run git commit. Finish with one sentence naming the bug.",
        20,
    ),
    "S2": (
        "Create a Python package `csvstats` in this empty repository: a module "
        "with a `column_stats(path) -> dict` function computing per-column "
        "min/max/mean for numeric CSV columns, a `python -m csvstats <file>` "
        "CLI printing them, and pytest tests including an edge case for "
        "non-numeric columns. Run the tests until green. Do not run git "
        "commit. Finish with one sentence stating the test count.",
        40,
    ),
    "S3": (
        "Answer these questions about this repository precisely, citing "
        "file:line for every claim. Do NOT modify any files.\n"
        "(a) Trace how a span token minted inside a digest is later resolved "
        "by `ctx get --span`: name every function on the path in order.\n"
        "(b) Which config field controls the cumulative session read budget, "
        "and in which function is it enforced?\n"
        "(c) List every call site of `snapshot_file` in src/.\n"
        "Finish with the three answers in a numbered list.",
        25,
    ),
    "S5": (
        "Audit src/ctx in this repository for HAND-ROLLED implementations "
        "that mature, well-maintained libraries could replace. The project "
        "has a strict dependency doctrine (see README.md and CONTRIBUTING.md): "
        "stdlib-first hot paths, opportunistic external binaries, optional "
        "pip extras with deterministic fallbacks — judge candidates against "
        "that doctrine, not against generic best practice. For each candidate "
        "report: the code (file:line range), what it hand-rolls, the specific "
        "replacement library, ADOPT or DECLINE with doctrine-consistent "
        "reasoning, and estimated replacement diff size. Do NOT modify any "
        "code. Finish with a ranked table of candidates (most valuable "
        "first), including the ones you recommend declining.",
        25,
    ),
    "S6": (
        "Bug hunt in src/ctx of this repository: find REAL defects — logic "
        "errors, race conditions, resource leaks, incorrect edge-case "
        "handling, broken invariants — not style issues. For each finding "
        "report: file:line, a one-paragraph explanation, a concrete failing "
        "scenario (specific inputs/state -> specific wrong behavior), and "
        "severity (high/medium/low). Verify each candidate by reading the "
        "surrounding code carefully; discard anything speculative. Do NOT "
        "modify any code. Aim for at least 5 findings; quality over "
        "quantity. Finish with the findings ranked by severity.",
        30,
    ),
    "S4": (
        "Do a tech-debt, documentation, and DevEx overhaul of this Python "
        "repository (straitjacket / ctx-harness):\n"
        "1. Run the test suite to establish a baseline.\n"
        "2. Add a GitHub Actions workflow at .github/workflows/nightly.yml "
        "that runs the test suite nightly on Python 3.12 with ripgrep "
        "installed, uploading a junit XML artifact.\n"
        "3. Write docs/ARCHITECTURE.md: a concise architecture overview you "
        "infer from the codebase (major modules, data flow, invariants).\n"
        "4. Find and fix at least three concrete tech-debt items in src/ctx. "
        "Keep changes minimal and safe; do not restructure modules.\n"
        "5. Re-run the full test suite and make sure it still passes.\n"
        "6. Do NOT run git commit or git push. Finish with a concise report.",
        80,
    ),
}


def make_fixture(scenario: str, dest: pathlib.Path, repo: pathlib.Path) -> None:
    dest.mkdir(parents=True)
    if scenario == "S1":
        for rel, content in SURGICAL_SRC.items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    elif scenario == "S2":
        pass  # empty repository
    elif scenario in ("S3", "S4", "S5", "S6"):
        # No pip install: tests/conftest.py puts src/ on sys.path, and the
        # `ctx` executable must stay bound to the host repo — an editable
        # install from a fixture clone would cross-link concurrent pairs.
        subprocess.run(["git", "clone", "-q", str(repo), str(dest / "r")], check=True)
        inner = dest / "r"
        subprocess.run(
            ["git", "checkout", "-q", current_branch(repo)], cwd=inner, check=True
        )
        return
    subprocess.run(["git", "init", "-q", "."], cwd=dest, check=True)


def current_branch(repo: pathlib.Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def workdir_for(scenario: str, base: pathlib.Path) -> pathlib.Path:
    return base / "r" if scenario in ("S3", "S4", "S5", "S6") else base


def run_pair(scenario: str, model: str, out: pathlib.Path, repo: pathlib.Path) -> None:
    task, max_turns = TASKS[scenario]
    procs = []
    for arm in ("naive", "sj"):
        base = out / f"{scenario}_{model}_{arm}"
        if base.exists():
            shutil.rmtree(base)
        make_fixture(scenario, base, repo)
        cfg = out / f"cc-{scenario}-{model}-{arm}"
        cfg.mkdir(parents=True, exist_ok=True)
        # PIP_REQUIRE_VIRTUALENV blocks a fixture agent from `pip install -e .`
        # hijacking the host's editable ctx install mid-benchmark (it happened:
        # an S4 overhaul agent re-pointed the global `ctx` at its own clone).
        env = {
            **os.environ,
            "CLAUDE_CONFIG_DIR": str(cfg),
            "PIP_REQUIRE_VIRTUALENV": "1",
        }
        argv = ["claude", "-p", task, "--max-turns", str(max_turns),
                "--output-format", "json", "--allowedTools", TOOLS]
        if MODELS[model]:
            argv += ["--model", MODELS[model]]
        if arm == "sj":
            argv = ["ctx", "wrap", "claude", "--proxy", "--"] + argv[1:]
        result_file = out / f"{scenario}_{model}_{arm}.json"
        err_file = out / f"{scenario}_{model}_{arm}.err"
        with open(result_file, "w") as rf, open(err_file, "w") as ef:
            procs.append(subprocess.Popen(
                argv, cwd=workdir_for(scenario, base), env=env,
                stdout=rf, stderr=ef,
            ))
    for p in procs:
        try:
            p.wait(timeout=2400)
        except subprocess.TimeoutExpired:
            p.kill()
    print(f"pair done: {scenario} {model}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--pairs", nargs="+", required=True,
                    help="scenario:model, e.g. S1:sonnet S1:haiku")
    ap.add_argument("--jobs", type=int, default=1,
                    help="pairs to run concurrently (each pair is 2 agents)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = []
        for pair in args.pairs:
            scenario, model = pair.split(":")
            futures.append(pool.submit(run_pair, scenario, model, args.out, args.repo))
        for f in futures:
            f.result()
    print("MATRIX_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
