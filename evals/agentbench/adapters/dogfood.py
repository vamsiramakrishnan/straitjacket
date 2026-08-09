#!/usr/bin/env python3
"""Dogfood adapter: open-ended missions against straitjacket's own repository.

The canary fixtures failed as a containment test because their flood was
defeatable with `| tail -30` -- the evidence sat at one end of the output. This
repository does not have that weakness. Its floods are SEARCH floods over 39k
lines across 102 source files:

    grep -rn 'def ' src/          1,247 lines /  96 KB
    grep -rn 'digest' src/ tests/ 1,020 lines /  93 KB
    git log --stat -30            1,278 lines /  70 KB

Matches are dispersed by construction, so head/tail/grep windows cannot win:
the agent must either carry the flood or route it through retrieval. That is
the regime straitjacket exists for, and it arises here from real work rather
than from a fixture we designed to favour ourselves.

Missions are open-ended, which makes grading the hard part. Each mission is
scored by a referee that re-derives the result from a PRISTINE checkout, never
from the agent's own account of what it did.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

REPO = pathlib.Path(os.environ.get("DOGFOOD_REPO", "/home/user/straitjacket")).resolve()
# The interpreter used to run the repo's suite. It needs the project's test
# dependencies; PYTHONPATH is pointed at the clone so imports resolve there and
# never at the original working tree.
TEST_PYTHON = os.environ.get("DOGFOOD_PYTHON", "python3")

RUNNER = """#!/bin/sh
# Run this repository's test suite. PYTHONPATH points at THIS checkout.
exec env PYTHONPATH="$(cd "$(dirname "$0")" && pwd)/src" {python} -m pytest -p no:cacheprovider "$@"
"""

BUGBASH_PROMPT = """\
You are bug-bashing this Python repository (the `ctx` / straitjacket context harness).

Find REAL defects in the source under `src/`. For each defect you find, write a
test that REPRODUCES it: a test that FAILS on the current code because the code
is wrong.

Rules:
- Put every test in a single new file: `tests/test_bugbash_findings.py`.
- Each test needs a docstring stating, concretely, the expected behaviour and
  the actual behaviour, and which function is at fault.
- Do NOT modify anything under `src/`. Do NOT modify or delete any existing
  test. This mission is to FIND and PROVE defects, not to fix them.
- Do not run `git commit`.
- Run the suite with `./run-tests.sh` (e.g. `./run-tests.sh -q tests/`).
  It is already configured for this checkout.
- A test that fails because you asserted something the code never promised is
  worthless. Read the docstrings and the surrounding contract first, and only
  assert behaviour the code is actually meant to have.

Prefer a few well-evidenced defects over many speculative ones. Finish with one
line: `FOUND: <number of defects you are confident in>`.
"""

MISSIONS = {
    "bugbash": {"prompt": BUGBASH_PROMPT, "findings_file": "tests/test_bugbash_findings.py"},
}


def load(n: int, **kw) -> list[dict]:
    names = [m for m in MISSIONS if not kw.get("mission") or m == kw["mission"]]
    return [{"id": f"dogfood/{m}", "mission": m} for m in names[: n or len(names)]]


def _git(args: list[str], cwd: pathlib.Path, check: bool = True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _clone(dest: pathlib.Path) -> str:
    """A local --shared clone of the repo's committed HEAD. Fast, and isolated."""
    subprocess.run(
        ["git", "clone", "-q", "--shared", str(REPO), str(dest)],
        capture_output=True, text=True, check=True, timeout=600,
    )
    (dest / "run-tests.sh").write_text(RUNNER.format(python=TEST_PYTHON), encoding="utf-8")
    (dest / "run-tests.sh").chmod(0o755)
    if not (dest / "ctx.toml").exists():
        (dest / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    _git(["add", "-A"], dest)
    _git(["-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "eval base"], dest)
    return _git(["rev-parse", "HEAD"], dest).stdout.strip()


def prepare(task: dict, workdir: pathlib.Path) -> str:
    if workdir.exists():
        shutil.rmtree(workdir)
    task["base_sha"] = _clone(workdir)
    return MISSIONS[task["mission"]]["prompt"]


def apply_gold(task: dict, workdir: pathlib.Path) -> None:
    """No gold for an open-ended mission.

    validate.py's contract cannot be satisfied here: there is no reference
    solution that must resolve. Grading is comparative between arms, not
    pass/fail against a key, so this adapter is deliberately not self-validating
    and validate.py will refuse it rather than pretend.
    """
    raise NotImplementedError(
        "dogfood missions are open-ended: no gold patch exists. "
        "Referee soundness here comes from re-deriving results in a pristine "
        "checkout, not from a gold control."
    )


NODE_RE = re.compile(r"^(?P<status>PASSED|FAILED|ERROR)\s+(?P<node>\S+)", re.MULTILINE)


def _run_suite(checkout: pathlib.Path, target: str, timeout: int = 1800) -> tuple[dict[str, str], str]:
    """Return {node: STATUS}. STATUS is kept verbatim -- collapsing it to a
    boolean is how a file that does not even parse gets scored as a proven
    defect: pytest reports `ERROR <file>` for a collection failure, and
    'not PASSED' reads that as a failing test."""
    proc = subprocess.run(
        ["./run-tests.sh", "-q", "--no-header", "-rA", "--tb=no", target],
        cwd=checkout, capture_output=True, text=True, timeout=timeout,
    )
    blob = proc.stdout + proc.stderr
    return {m.group("node"): m.group("status") for m in NODE_RE.finditer(blob)}, blob


def _changed(workdir: pathlib.Path, pathspec: str) -> list[str]:
    out = _git(["status", "--porcelain", "--", pathspec], workdir, check=False).stdout
    return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]


def grade(task: dict, workdir: pathlib.Path, **kw) -> dict:
    """Re-derive the result in a pristine checkout. Never trust the agent's account.

    A finding counts only if the test the agent wrote FAILS on untouched HEAD --
    that is what makes it a reproduction rather than a claim. The agent's own
    prose, and its FOUND: line, are recorded but never scored.
    """
    findings = MISSIONS[task["mission"]]["findings_file"]
    src_touched = _changed(workdir, "src")
    tests_touched = [p for p in _changed(workdir, "tests") if findings not in p]
    findings_path = workdir / findings

    result = {
        "resolved": False,
        "reproduced": 0,
        "claimed_tests": 0,
        "invalid": 0,
        "src_modified": len(src_touched),
        "existing_tests_modified": len(tests_touched),
        "tests_tampered": bool(src_touched or tests_touched),
        "findings_written": findings_path.exists(),
    }
    if not findings_path.exists():
        return result

    # Pristine checkout at the same base: the agent's edits cannot leak in.
    pristine = workdir.parent / (workdir.name + "_pristine")
    if pristine.exists():
        shutil.rmtree(pristine)
    _clone(pristine)
    _git(["checkout", "-q", task["base_sha"]], pristine, check=False)

    dest = pristine / findings
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(findings_path, dest)

    try:
        nodes, blob = _run_suite(pristine, findings)
    except subprocess.TimeoutExpired:
        result["invalid"] = -1
        return result

    # Only a real test node (file::test_name) that FAILED counts as a
    # reproduction. ERROR means the test itself is broken -- an import failure,
    # a bad fixture, a file that does not parse -- and proves nothing about the
    # source. Counting ERROR as a defect lets an agent score by writing garbage.
    tests = {n: s for n, s in nodes.items() if "::" in n}
    result["claimed_tests"] = len(tests)
    result["reproduced"] = sum(1 for s in tests.values() if s == "FAILED")
    result["errored_tests"] = sum(1 for s in tests.values() if s == "ERROR")
    result["invalid"] = result["errored_tests"] + (0 if tests else 1)
    if not tests:
        # Collection error, import failure, syntax error: nothing was proved.
        result["collect_error"] = blob[-600:]

    # "resolved" for this mission means: at least one reproduction, and the
    # agent stayed inside the mission's rules.
    result["resolved"] = bool(result["reproduced"] > 0 and not result["tests_tampered"])
    result["diff_stat"] = _git(["diff", "--stat"], workdir, check=False).stdout[-400:]
    shutil.rmtree(pristine, ignore_errors=True)
    return result
