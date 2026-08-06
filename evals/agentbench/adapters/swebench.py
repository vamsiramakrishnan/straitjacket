#!/usr/bin/env python3
"""SWE-bench Verified adapter.

Verified rather than Pro, deliberately: 500 human-filtered instances with a
mature public harness and official per-instance environment images. The Pro
copy that started this work ships `FAIL_TO_PASS: []` and `PASS_TO_PASS: []` for
every row, so no grader -- correct or not -- could ever have scored it.

The agent works in a real checkout: it runs the suite itself, floods itself, and
navigates itself. That is the mode a fixed model ladder cannot reach, and the
reason this harness exists alongside `evals/tokenomics/`.

Environments
------------
`--adapter-arg exec=local` runs the suite with `--adapter-arg python=<path>`.
Fast, no docker, but YOU are responsible for the interpreter having the
instance's dependencies. Wrong env shows up as `p2p` failures at baseline,
which `validate.py` reports rather than hides.

`--adapter-arg exec=docker` runs the suite inside the official per-instance
image (`swebench/sweb.eval.x86_64.<instance_id>`). This is the path that gives
comparable numbers.

Whichever you pick, run `python evals/agentbench/validate.py --adapter swebench`
FIRST. It drives gold/baseline/tampered/vandal through grade() with no model
involved; if gold does not resolve, the environment is wrong and every paid arm
run against it would be noise.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE.parent / "_cache"
DATASET = "princeton-nlp/SWE-bench_Verified"
ROWS_URL = "https://datasets-server.huggingface.co/rows"

PROMPT_TEMPLATE = (
    "You are working in a checkout of {repo}. Resolve the following issue by editing the "
    "source. The repository's test suite is present; run it to check your work.\n\n"
    "Do NOT edit, delete, or disable any test file. Do not run git commit.\n\n"
    "--- ISSUE ---\n{problem}\n"
)


def _fetch_rows(offset: int, length: int) -> list[dict]:
    q = urllib.parse.urlencode(
        {"dataset": DATASET, "config": "default", "split": "test",
         "offset": offset, "length": length}
    )
    with urllib.request.urlopen(f"{ROWS_URL}?{q}", timeout=120) as r:
        doc = json.loads(r.read())
    return [row["row"] for row in doc.get("rows", [])]


def load(n: int, **kw) -> list[dict]:
    """Fetch (and cache) instances. `difficulty=` filters, e.g. '<15 min fix'."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / "swebench_verified.json"
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        rows, offset = [], 0
        while offset < 500:
            batch = _fetch_rows(offset, 100)
            if not batch:
                break
            rows.extend(batch)
            offset += len(batch)
        cache.write_text(json.dumps(rows), encoding="utf-8")

    if kw.get("difficulty"):
        rows = [r for r in rows if r.get("difficulty") == kw["difficulty"]]
    if kw.get("repo"):
        rows = [r for r in rows if r.get("repo") == kw["repo"]]

    # Deterministic order; stratification is a separate, explicit step.
    rows.sort(key=lambda r: r["instance_id"])
    out = []
    for r in rows[:n]:
        out.append({
            "id": r["instance_id"],
            "repo": r["repo"],
            "base_commit": r["base_commit"],
            "problem_statement": r["problem_statement"],
            "patch": r["patch"],
            "test_patch": r["test_patch"],
            "f2p": json.loads(r["FAIL_TO_PASS"]) if isinstance(r["FAIL_TO_PASS"], str) else r["FAIL_TO_PASS"],
            "p2p": json.loads(r["PASS_TO_PASS"]) if isinstance(r["PASS_TO_PASS"], str) else r["PASS_TO_PASS"],
            "env_commit": r.get("environment_setup_commit"),
        })
    return out


def _git(args: list[str], cwd: pathlib.Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def prepare(task: dict, workdir: pathlib.Path) -> str:
    """Materialize the repo at base_commit with the instance's test files applied."""
    url = f"https://github.com/{task['repo']}.git"
    subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(workdir)],
        capture_output=True, text=True, check=True, timeout=1800,
    )
    _git(["checkout", "-q", task["base_commit"]], workdir)

    # The test files belong to the instance, not to the agent's solution: apply
    # them before the session and treat any later change to them as tampering.
    if task.get("test_patch"):
        patch_file = workdir / ".instance_test.patch"
        patch_file.write_text(task["test_patch"], encoding="utf-8")
        _git(["apply", "--whitespace=nowarn", str(patch_file)], workdir)
        patch_file.unlink()

    (workdir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    _git(["add", "-A"], workdir)
    _git(["-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "instance base"], workdir)
    return PROMPT_TEMPLATE.format(repo=task["repo"], problem=task["problem_statement"])


def apply_gold(task: dict, workdir: pathlib.Path) -> None:
    """Apply the reference patch. For validate.py only -- never the harness."""
    patch_file = workdir / ".gold.patch"
    patch_file.write_text(task["patch"], encoding="utf-8")
    _git(["apply", "--whitespace=nowarn", str(patch_file)], workdir)
    patch_file.unlink()


NODE_RE = re.compile(r"^(?P<status>PASSED|FAILED|ERROR)\s+(?P<node>\S+)", re.MULTILINE)


def _pytest_argv(nodes: list[str], python: str) -> list[str]:
    return [python, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header",
            "-rA", "--tb=no", *nodes]


def _run_nodes(task: dict, workdir: pathlib.Path, nodes: list[str], **kw) -> dict[str, bool]:
    if not nodes:
        return {}
    exec_mode = kw.get("exec", os.environ.get("AGENTBENCH_EXEC", "local"))
    python = kw.get("python", "python3")

    if exec_mode == "docker":
        image = f"swebench/sweb.eval.x86_64.{task['id'].replace('__', '_1776_')}:latest"
        argv = ["docker", "run", "--rm", "-v", f"{workdir}:/testbed", "-w", "/testbed",
                image, *_pytest_argv(nodes, "python")]
    else:
        argv = _pytest_argv(nodes, python)

    proc = subprocess.run(argv, cwd=workdir, capture_output=True, text=True, timeout=1800)
    blob = proc.stdout + proc.stderr
    seen = {m.group("node"): m.group("status") == "PASSED" for m in NODE_RE.finditer(blob)}
    # Unreported node == not passed. Silence is never success.
    return {n: seen.get(n, False) for n in nodes}


def _tests_modified(task: dict, workdir: pathlib.Path) -> bool:
    """Any change to a path the instance's test_patch touched is tampering."""
    touched = re.findall(r"^\+\+\+ b/(\S+)", task.get("test_patch") or "", re.MULTILINE)
    if not touched:
        touched = ["tests", "test"]
    proc = _git(["status", "--porcelain", "--", *touched], workdir, check=False)
    return bool(proc.stdout.strip())


def grade(task: dict, workdir: pathlib.Path, **kw) -> dict:
    f2p = _run_nodes(task, workdir, task["f2p"], **kw)
    p2p = _run_nodes(task, workdir, task["p2p"], **kw)
    f2p_ok, p2p_ok = sum(f2p.values()), sum(p2p.values())
    tampered = _tests_modified(task, workdir)
    return {
        "resolved": bool(f2p_ok == len(f2p) and p2p_ok == len(p2p) and not tampered),
        "f2p": f"{f2p_ok}/{len(f2p)}",
        "p2p": f"{p2p_ok}/{len(p2p)}",
        "tests_tampered": tampered,
    }
