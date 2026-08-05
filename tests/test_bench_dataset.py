"""Stabilization gate for straitjacket-bench (evals/bench/dataset.py).

A benchmark is only trustworthy if its labels are. For every scenario this
asserts the grader is *sound*: the known **gold** solution scores success,
and a **no-op** (unedited fixture / empty answer) scores failure. If a
grader can be satisfied without solving the task, or the gold cannot
satisfy it, the scenario is broken — caught here, not in a live run.

Uses ``sys.executable`` (the test interpreter, which carries pytest) as the
grader's pytest — the same interpreter-availability discipline the runner
enforces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from bench.dataset import SCENARIOS  # noqa: E402

PYTEST_PY = sys.executable


# Gold solvers: transform the built fixture into a passing state, or return
# the gold answer text. Keyed by scenario id.
def _gold_answer(root: Path, sid: str) -> str:
    if sid == "comp-refs":
        return "SITES: 2"
    if sid == "comp-impact":
        return "FILES: server.py, worker.py"
    if sid == "comp-nav":
        return "ANSWER: -1"
    if sid == "comp-grep":
        gold = json.loads((root / "_gold.json").read_text())
        return "FILES: " + ", ".join(gold)
    if sid == "comp-trace":
        return "PATH: endpoint > handle > persist"
    if sid == "data-aggregate":
        return "COUNT: " + str(json.loads((root / "_gold.json").read_text()))
    return ""


def _gold_edit(root: Path, sid: str) -> None:
    if sid == "bug-regression":
        (root / "orders/pricing.py").write_text(
            "def discount(amount, pct):\n"
            "    if not 0 <= pct <= 100:\n"
            "        raise ValueError('pct out of range')\n"
            "    return amount * (1 - pct / 100.0)\n", encoding="utf-8")
    elif sid == "bug-flood":
        (root / "lib/calc.py").write_text(
            "def scale(x, k):\n    return x * k\n\n"
            "def normalize(xs):\n    total = sum(xs) or 1\n"
            "    return [x / total for x in xs]\n", encoding="utf-8")
    elif sid == "bug-multi":
        (root / "app/strings.py").write_text(
            "def shout(s):\n    return s.upper() + '!'\n", encoding="utf-8")
        (root / "app/nums.py").write_text(
            "def half(n):\n    return n // 2\n", encoding="utf-8")
        (root / "app/lists.py").write_text(
            "def first(xs):\n    return xs[0]\n", encoding="utf-8")
    elif sid == "bug-subtle":
        (root / "geo/box.py").write_text(
            "def area(width, height):\n    return width * height\n\n"
            "def perimeter(width, height):\n    return 2 * (width + height)\n",
            encoding="utf-8")
    elif sid == "bug-import":
        (root / "svc/helpers.py").write_text(
            "def clean(s):\n    return s.strip()\n\n"
            "def tidy(s):\n    return s.strip()\n", encoding="utf-8")
    elif sid == "impl-feature":
        with (root / "util/nums.py").open("a", encoding="utf-8") as fh:
            fh.write("\ndef clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n")
    elif sid == "impl-test":
        (root / "test_email.py").write_text(
            "from acct.email import normalize_email\n\n"
            "def test_empty():\n    assert normalize_email('') == ''\n"
            "def test_ws():\n    assert normalize_email('  A@B.COM ') == 'a@b.com'\n",
            encoding="utf-8")
    elif sid == "refactor-rename":
        (root / "pkg/core.py").write_text(
            "def newName(x):\n    return x * 2\n", encoding="utf-8")
        (root / "pkg/use.py").write_text(
            "from pkg.core import newName\n\n"
            "def double_all(xs):\n    return [newName(x) for x in xs]\n",
            encoding="utf-8")


def _apply_gold(root: Path, sid: str) -> str:
    """Return the gold final-message text (for answer scenarios) after
    applying any gold source edit (for suite scenarios)."""
    _gold_edit(root, sid)
    return _gold_answer(root, sid)


@pytest.mark.parametrize("scn", SCENARIOS, ids=lambda s: s.id)
def test_gold_passes_and_noop_fails(scn, tmp_path):
    # No-op: the unedited fixture with an empty answer must FAIL the grader
    # (else the task is trivially satisfiable and the scenario is worthless).
    noop_root = tmp_path / "noop"
    noop_root.mkdir()
    scn.build(noop_root)
    noop = scn.grade(noop_root, "", PYTEST_PY)
    assert noop["success"] is False, f"{scn.id}: no-op unexpectedly succeeded ({noop})"

    # Gold: the known solution must PASS the grader.
    gold_root = tmp_path / "gold"
    gold_root.mkdir()
    scn.build(gold_root)
    gold_text = _apply_gold(gold_root, scn.id)
    gold = scn.grade(gold_root, gold_text, PYTEST_PY)
    assert gold["success"] is True, f"{scn.id}: gold solution failed the grader ({gold})"


def test_dataset_is_diverse_and_labeled():
    cats = {s.category for s in SCENARIOS}
    floods = {s.flood for s in SCENARIOS}
    assert cats == {"bug", "comprehension", "implement", "refactor", "data"}
    assert floods == {"none", "medium", "high"}
    assert len(SCENARIOS) >= 12
    assert len({s.id for s in SCENARIOS}) == len(SCENARIOS)  # unique ids
    # at least three genuinely flood-bearing scenarios (straitjacket's turf)
    assert sum(1 for s in SCENARIOS if s.flood == "high") >= 3
