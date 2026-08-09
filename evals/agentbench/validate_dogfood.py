#!/usr/bin/env python3
"""Referee controls for the dogfood adapter.

`validate.py` cannot cover this adapter: its contract is gold-must-resolve, and
an open-ended mission has no gold patch. Soundness here comes from a different
place -- the referee re-derives every finding in a PRISTINE checkout and never
reads the agent's own account -- so it needs its own controls.

Six states, no model involved. The important ones are the adversarial cases:
an agent that cannot win by writing garbage, by breaking imports, by editing
the source it was asked only to examine, or by asserting things that were
already true.

    A  a genuinely failing test + a passing one -> exactly 1 reproduced
    B  edited src/ (mission violation)          -> must NOT resolve
    C  file with a syntax error                 -> invalid, must NOT resolve
    D  no findings file written                 -> must NOT resolve
    E  test file with a bad import              -> invalid, must NOT resolve
    G  only passing tests                       -> 0 reproduced, must NOT resolve

C and E are the ones that matter most. pytest reports `ERROR <file>` for a
collection failure, so a referee that scores "not PASSED" as a defect will
count a file that does not even parse as a proven bug. That bug was live in
this adapter until these controls caught it.

Usage:
    DOGFOOD_PYTHON=/path/to/venv/bin/python python evals/agentbench/validate_dogfood.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from adapters import dogfood  # noqa: E402

FINDINGS = "tests/test_bugbash_findings.py"

CASES = [
    ("A real-failure + passing test -> 1 reproduced",
     'def test_a():\n    """expected 2, actual 1"""\n    assert 1 == 2\n\ndef test_b():\n    assert True\n',
     None,
     lambda r: r["reproduced"] == 1 and r["claimed_tests"] == 2 and r["resolved"]),
    ("B edited src/ -> must NOT resolve",
     "def test_x():\n    assert 1 == 2\n",
     "src",
     lambda r: r["src_modified"] == 1 and not r["resolved"]),
    ("C syntax error -> invalid, must NOT resolve",
     "def test_broken(:\n",
     None,
     lambda r: r["invalid"] >= 1 and r["reproduced"] == 0 and not r["resolved"]),
    ("D no findings file -> must NOT resolve",
     None,
     None,
     lambda r: not r["findings_written"] and not r["resolved"]),
    ("E bad import -> invalid, must NOT resolve",
     "import nonexistent_module_xyz\n\ndef test_x():\n    assert 1 == 2\n",
     None,
     lambda r: r["reproduced"] == 0 and not r["resolved"]),
    ("G only passing tests -> 0 reproduced, must NOT resolve",
     "def test_only_passes():\n    assert True\n",
     None,
     lambda r: r["reproduced"] == 0 and not r["resolved"]),
]


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix="dogfood_ctrl_") as td:
        root = pathlib.Path(td)
        for i, (label, body, tamper, predicate) in enumerate(CASES):
            task = {"id": "dogfood/bugbash", "mission": "bugbash"}
            workdir = root / f"case{i}"
            dogfood.prepare(task, workdir)
            if body is not None:
                (workdir / FINDINGS).write_text(body, encoding="utf-8")
            if tamper == "src":
                p = workdir / "src" / "ctx" / "__init__.py"
                p.write_text(p.read_text() + "\n# tampered\n", encoding="utf-8")

            r = dogfood.grade(task, workdir)
            ok = predicate(r)
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'} {label}\n"
                  f"        reproduced={r['reproduced']} claimed={r['claimed_tests']} "
                  f"invalid={r['invalid']} resolved={r['resolved']} src_mod={r['src_modified']}",
                  flush=True)
            shutil.rmtree(workdir, ignore_errors=True)

    total = len(CASES)
    print(f"\ndogfood referee controls: {total - failures}/{total} passed")
    if failures:
        print("REFEREE IS NOT SOUND -- do not run paid arms against it")
        return 1
    print("referee is sound: only a FAILED real test node counts as a reproduction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
