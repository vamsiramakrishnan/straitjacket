#!/usr/bin/env python3
"""Canary adapter: SWE-shaped fixtures that run anywhere, with no docker.

Purpose is instrument validation, not a headline. Each fixture is a real git
repo with a real bug, a real failing test, and a gold patch -- the same
prepare/grade contract the swebench adapter implements -- but small enough to
run in a review sandbox in seconds.

Two of the fixtures are deliberately NOISY: the suite prints thousands of lines
around the failure, so the arms actually differ. `quiet` is the low-output
control that BENCHMARK.md insists on: if a harness only ever sees floods it
will flatter itself, and the tiny-surgical-task regression that produced
graduated engagement is exactly what a control catches.

Grading runs the same FAIL_TO_PASS / PASS_TO_PASS split SWE-bench uses, so
`validate.py` can prove the referee on these before anyone spends on the real
corpus.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import textwrap

PROMPT = (
    "This repository has a failing test suite. Run the tests, find the bug, and fix it. "
    "Do not edit or delete any file under tests/. Do not run git commit. "
    "Finish when the suite passes."
)

# Each fixture: source files, a test file, the failing/passing test node ids,
# and the gold patch expressed as the corrected source.
FIXTURES: dict[str, dict] = {
    "quiet": {
        "noise": 0,
        "src": textwrap.dedent("""\
            def normalize(values):
                \"\"\"Scale values into [0, 1]. Empty input returns [].\"\"\"
                if not values:
                    return []
                lo, hi = min(values), max(values)
                if hi == lo:
                    return [0.0 for _ in values]
                return [(v - lo) / (hi - lo) for v in values]
            """),
        "bug": ("return [(v - lo) / (hi - lo) for v in values]",
                "return [(v - lo) / hi for v in values]"),
        "f2p": ["tests/test_norm.py::test_negative_range"],
        "p2p": ["tests/test_norm.py::test_empty", "tests/test_norm.py::test_flat"],
        "tests": textwrap.dedent("""\
            from pkg.norm import normalize

            def test_empty():
                assert normalize([]) == []

            def test_flat():
                assert normalize([5, 5, 5]) == [0.0, 0.0, 0.0]

            def test_negative_range():
                assert normalize([-4, 0, 4]) == [0.0, 0.5, 1.0]
            """),
        "module": "norm",
    },
    "flood": {
        "noise": 4000,
        "src": textwrap.dedent("""\
            def bucket(items, size):
                \"\"\"Split items into consecutive chunks of length `size`.

                The final chunk may be shorter. `size` must be >= 1.
                \"\"\"
                if size < 1:
                    raise ValueError("size must be >= 1")
                out = []
                for i in range(0, len(items), size):
                    out.append(items[i:i + size])
                return out
            """),
        "bug": ("for i in range(0, len(items), size):",
                "for i in range(0, len(items) - size + 1, size):"),
        "f2p": ["tests/test_bucket.py::test_ragged_tail"],
        "p2p": ["tests/test_bucket.py::test_even", "tests/test_bucket.py::test_bad_size"],
        "tests": textwrap.dedent("""\
            import pytest
            from pkg.bucket import bucket

            def test_even():
                assert bucket([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

            def test_bad_size():
                with pytest.raises(ValueError):
                    bucket([1], 0)

            def test_ragged_tail():
                assert bucket([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
            """),
        "module": "bucket",
    },
    "deep": {
        "noise": 2500,
        "src": textwrap.dedent("""\
            def _depth(node):
                if node is None:
                    return 0
                return 1 + max(_depth(node.get("l")), _depth(node.get("r")))


            def balanced(tree):
                \"\"\"True when no sibling subtrees differ in depth by more than 1.\"\"\"
                if tree is None:
                    return True
                if abs(_depth(tree.get("l")) - _depth(tree.get("r"))) > 1:
                    return False
                return balanced(tree.get("l")) and balanced(tree.get("r"))
            """),
        "bug": ("return balanced(tree.get(\"l\")) and balanced(tree.get(\"r\"))",
                "return True"),
        "f2p": ["tests/test_tree.py::test_deep_imbalance"],
        "p2p": ["tests/test_tree.py::test_none", "tests/test_tree.py::test_shallow"],
        "tests": textwrap.dedent("""\
            from pkg.tree import balanced

            def test_none():
                assert balanced(None) is True

            def test_shallow():
                assert balanced({"l": None, "r": None}) is True

            def test_deep_imbalance():
                # Both halves are depth 3, so the ROOT check passes and only the
                # recursive descent can find the imbalance -- which is the line
                # the bug removes. A tree caught at the root would leave the bug
                # unreachable and the fixture would resolve at baseline.
                leaf = {"l": None, "r": None}
                a2 = {"l": leaf, "r": None}          # depth 2, balanced
                bad = {"l": a2, "r": None}           # depth 3, imbalanced (2 vs 0)
                good = {"l": a2, "r": leaf}          # depth 3, balanced (2 vs 1)
                assert balanced({"l": bad, "r": good}) is False
            """),
        "module": "tree",
    },
}


def load(n: int, **kw) -> list[dict]:
    names = list(FIXTURES)
    return [{"id": f"canary/{name}", "fixture": name} for name in names[:n] or names]


def _conftest(noise: int) -> str:
    """Emit `noise` lines of plausible chatter around every test."""
    if noise <= 0:
        return ""
    return textwrap.dedent(f"""\
        import pytest

        NOISE = {noise}

        @pytest.fixture(autouse=True)
        def _chatter(request):
            for i in range(NOISE):
                print(f"[worker-{{i % 17}}] step {{i}} ok checksum=0x{{i * 2654435761 & 0xffffffff:08x}}")
            yield
        """)


def prepare(task: dict, workdir: pathlib.Path) -> str:
    spec = FIXTURES[task["fixture"]]
    pkg = workdir / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    good, bad = spec["bug"]
    buggy = spec["src"].replace(good, bad)
    if buggy == spec["src"]:
        raise RuntimeError(f"{task['id']}: bug injection did not apply")
    (pkg / f"{spec['module']}.py").write_text(buggy, encoding="utf-8")

    tests = workdir / "tests"
    tests.mkdir()
    (tests / f"test_{spec['module']}.py").write_text(spec["tests"], encoding="utf-8")
    conf = _conftest(spec["noise"])
    if conf:
        (tests / "conftest.py").write_text(conf, encoding="utf-8")

    # Both arms get the same tree shape, including ctx.toml and git.
    (workdir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    # Without this, pytest's __pycache__ lands untracked under tests/ and the
    # tamper check fires on every run, including the gold control.
    (workdir / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=workdir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True)
    subprocess.run(
        ["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "base"],
        cwd=workdir, check=True,
    )
    return PROMPT


def apply_gold(task: dict, workdir: pathlib.Path) -> None:
    """Write the corrected source. Used by validate.py, never by the harness."""
    spec = FIXTURES[task["fixture"]]
    (workdir / "pkg" / f"{spec['module']}.py").write_text(spec["src"], encoding="utf-8")


# pytest -rA emits "STATUS node", not "node STATUS". Getting this backwards
# silently scores every node as absent -> False, which reads as "nothing passed"
# rather than as a parse failure. validate.py exists to catch exactly that.
NODE_RE = re.compile(r"^(?P<status>PASSED|FAILED|ERROR)\s+(?P<node>\S+)", re.MULTILINE)


def _run_nodes(workdir: pathlib.Path, nodes: list[str]) -> dict[str, bool]:
    if not nodes:
        return {}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header",
         "-rA", "--tb=no", *nodes],
        cwd=workdir, capture_output=True, text=True, timeout=300,
    )
    blob = proc.stdout + proc.stderr
    seen = {m.group("node"): m.group("status") == "PASSED" for m in NODE_RE.finditer(blob)}
    # A node pytest never reported (collection error, import failure) is a fail,
    # not a missing value -- silence must never read as success.
    return {n: seen.get(n, False) for n in nodes}


def grade(task: dict, workdir: pathlib.Path) -> dict:
    spec = FIXTURES[task["fixture"]]
    f2p = _run_nodes(workdir, spec["f2p"])
    p2p = _run_nodes(workdir, spec["p2p"])
    f2p_ok = sum(f2p.values())
    p2p_ok = sum(p2p.values())
    tampered = _tests_modified(workdir)
    return {
        "resolved": bool(f2p_ok == len(f2p) and p2p_ok == len(p2p) and not tampered),
        "f2p": f"{f2p_ok}/{len(f2p)}",
        "p2p": f"{p2p_ok}/{len(p2p)}",
        "tests_tampered": tampered,
    }


def _tests_modified(workdir: pathlib.Path) -> bool:
    """An agent that edits the tests has not fixed the bug."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "tests"],
        cwd=workdir, capture_output=True, text=True,
    )
    return bool(proc.stdout.strip())
