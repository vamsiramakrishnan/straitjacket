"""straitjacket-bench: a diverse, stabilized coding-task dataset.

Fourteen scenarios spanning the coding-task taxonomy — bug-fix (small,
flood, multi, subtle, build-break), comprehension (references, impact,
large-file navigation, grep-across-tree, dataflow trace), implementation
(feature, test-authoring), refactor (rename), and data transformation.
Several are deliberately **flood-bearing** (a big test suite, a huge file,
a large log) — straitjacket's home turf — and several are **small no-flood**
tasks where containment is pure overhead: the honest full spread, not a
cherry-picked win.

Each scenario is model-free gradeable. Two grading styles:

* **suite** — the fixture ships a pytest suite; success = green after the
  agent's edits (plus per-scenario invariants, e.g. "didn't delete tests").
* **answer** — the agent must end its final message with a structured tag
  (``SITES: 4``, ``FILES: a.py,b.py``, ``COUNT: 37``, ``PATH: a>b>c``),
  graded by exact / set / ordered-subsequence match against a known gold.

Determinism: fixtures are built from fixed seeds and committed structure;
each (scenario, arm, rep) runs in an isolated directory; gold answers are
computed, not guessed. Live agents are non-deterministic — the runner does
N repeats and the report declares variance. Grading itself is deterministic.

The SWE-bench technique borrowed: *a real failing test defines success*.
We synthesize the failures (controlled, so grading needs no gold patch and
no model) rather than mine them, which keeps the suite hermetic and the
label exact — the charter's "external corpora are teachers, never referees"
(evals/BENCHMARK.md) applied by building teachers we fully control.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str  # bug | comprehension | implement | refactor | data
    flood: str  # none | medium | high
    prompt: str
    build: Callable[[Path], None]
    grade: Callable[[Path, str, str], dict]  # (root, final_text, pytest_py) -> {...}
    turn_cap: int = 25
    tags: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------------ helpers
def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _base(root: Path) -> None:
    _write(root, {"ctx.toml": "version = 1\n",
                  ".gitignore": "__pycache__/\n.pytest_cache/\n.ctx*/\n"})


def _answer(text: str, tag: str) -> str | None:
    m = re.search(rf"{tag}:\s*(.+)", text or "")
    return m.group(1).strip() if m else None


def _suite_green(root: Path, pytest_py: str, target: str = "") -> tuple[bool, str]:
    argv = [pytest_py, "-m", "pytest", "-q"]
    if target:
        argv.append(target)
    try:
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"pytest failed to run: {e}"
    tail = (r.stdout or r.stderr).strip().splitlines()
    return r.returncode == 0, (tail[-1] if tail else "")


def _files_set(answer: str | None) -> set[str]:
    if not answer:
        return set()
    parts = re.split(r"[,\s]+", answer.strip())
    return {Path(p.strip()).name for p in parts if p.strip()}


def _suite_grade(invariant=None):
    """A grader closure: success = suite green, with an optional extra
    invariant(root)->bool (e.g. tests not deleted, old name gone)."""

    def grade(root: Path, final_text: str, pytest_py: str) -> dict:
        green, line = _suite_green(root, pytest_py)
        ok_inv = True if invariant is None else bool(invariant(root))
        return {"success": bool(green and ok_inv), "suite": line,
                "invariant_ok": ok_inv}

    return grade


# =============================================================== BUG-FIX (5)
def _b_regression(root: Path) -> None:
    _base(root)
    _write(root, {
        "orders/__init__.py": "",
        "orders/pricing.py":
            "def discount(amount, pct):\n"
            "    if not 0 <= pct <= 100:\n"
            "        raise ValueError('pct out of range')\n"
            "    return amount * (1 - pct / 100.0)\n",
        "test_pricing.py":
            "from orders.pricing import discount\n\n"
            "def test_ten_pct():\n    assert discount(100, 10) == 90.0\n"
            "def test_full():\n    assert discount(50, 100) == 0.0\n",
    })
    # regression: /100 -> /10 (a raise-free wrong value)
    (root / "orders/pricing.py").write_text(
        "def discount(amount, pct):\n"
        "    if not 0 <= pct <= 100:\n"
        "        raise ValueError('pct out of range')\n"
        "    return amount * (1 - pct / 10.0)\n", encoding="utf-8")


def _b_flood(root: Path) -> None:
    """A LARGE noisy suite (60 tests, verbose helpers) with ONE seeded
    regression — the flood case: raw pytest output is a wall, the bug is
    one line in it."""
    _base(root)
    calc = ["def scale(x, k):", "    return x * k", "",
            "def normalize(xs):", "    total = sum(xs) or 1",
            "    return [x / total for x in xs]", ""]
    _write(root, {"lib/__init__.py": "", "lib/calc.py": "\n".join(calc)})
    rng = random.Random(7)
    tests = ["from lib.calc import scale, normalize\n"]
    for i in range(60):
        a, b = rng.randint(1, 9), rng.randint(1, 9)
        tests.append(f"def test_scale_{i:02d}():\n    assert scale({a}, {b}) == {a*b}\n")
    tests.append("def test_normalize_sums_to_one():\n"
                 "    assert abs(sum(normalize([1, 3])) - 1.0) < 1e-9\n")
    _write(root, {"test_calc.py": "\n".join(tests)})
    # regression: scale multiplies wrong (x*k -> x+k) → ~half the tests fail
    (root / "lib/calc.py").write_text(
        "def scale(x, k):\n    return x + k\n\n"
        "def normalize(xs):\n    total = sum(xs) or 1\n"
        "    return [x / total for x in xs]\n", encoding="utf-8")


def _b_multi(root: Path) -> None:
    _base(root)
    _write(root, {
        "app/__init__.py": "",
        "app/strings.py": "def shout(s):\n    return s.lower() + '!'\n",  # bug: lower
        "app/nums.py": "def half(n):\n    return n * 2\n",  # bug: *2
        "app/lists.py": "def first(xs):\n    return xs[-1]\n",  # bug: last
        "test_all.py":
            "from app.strings import shout\nfrom app.nums import half\n"
            "from app.lists import first\n\n"
            "def test_shout():\n    assert shout('hi') == 'HI!'\n"
            "def test_half():\n    assert half(10) == 5\n"
            "def test_first():\n    assert first([1,2,3]) == 1\n",
    })


def _b_subtle(root: Path) -> None:
    _base(root)
    _write(root, {
        "geo/__init__.py": "",
        "geo/box.py":
            "def area(width, height):\n    return width + height\n"  # bug: + not *
            "\ndef perimeter(width, height):\n    return 2 * (width + height)\n",
        "test_box.py":
            "from geo.box import area, perimeter\n\n"
            "def test_area():\n    assert area(3, 4) == 12\n"
            "def test_perimeter():\n    assert perimeter(3, 4) == 14\n",
    })


def _b_import(root: Path) -> None:
    _base(root)
    _write(root, {
        "svc/__init__.py": "",
        "svc/core.py": "from svc.helpers import tidy\n\n"
                       "def run(s):\n    return tidy(s)\n",
        # helpers.py is MISSING the referenced name (build break)
        "svc/helpers.py": "def clean(s):\n    return s.strip()\n",
        "test_core.py":
            "from svc.core import run\n\n"
            "def test_run():\n    assert run('  hi ') == 'hi'\n",
    })


# ========================================================= COMPREHENSION (5)
def _c_refs(root: Path) -> None:
    _base(root)
    _write(root, {
        "billing/__init__.py": "",
        "billing/gateway.py": "def charge(card, amount):\n    return {'ok': True}\n",
        "billing/checkout.py":
            "from billing.gateway import charge\n\n"
            "def buy(card, cart):\n    total = sum(cart)\n"
            "    return charge(card, total)\n",  # real call
        "billing/refund.py":
            "from billing.gateway import charge\n\n"
            "# to refund we do NOT charge again; see charge() docs\n"  # decoy comment
            "def refund(card, amount):\n"
            "    note = 'reverse the charge'\n"  # decoy string
            "    return charge(card, -amount)\n",  # real call
        "test_billing.py":
            "from billing.checkout import buy\n\n"
            "def test_buy():\n    assert buy('c', [1,2])['ok']\n",
    })
    # gold: charge is CALLED at 2 sites (checkout.buy, refund.refund).
    # definition + import lines + comment/string are NOT calls.


def _c_impact(root: Path) -> None:
    _base(root)
    _write(root, {
        "conf/__init__.py": "",
        "conf/config.py": "class Config:\n    @staticmethod\n"
                          "    def load(path):\n        return {'path': path}\n",
        "conf/server.py": "from conf.config import Config\n\n"
                          "def boot():\n    return Config.load('/etc/s')\n",
        "conf/worker.py": "from conf.config import Config\n\n"
                          "def start():\n    return Config.load('/etc/w')\n",
        "conf/unused.py": "# imports Config but never calls load\n"
                          "from conf.config import Config  # noqa\n",
    })
    # gold impacted files (call Config.load): server.py, worker.py


def _c_nav(root: Path) -> None:
    """A large generated file — reading it whole is a flood; the answer is
    one function's behavior."""
    _base(root)
    lines = ['"""A large module."""', ""]
    rng = random.Random(11)
    for i in range(400):
        lines += [f"def noise_{i:03d}(a, b):",
                  f"    # helper {i}", f"    return a * {rng.randint(1,9)} + b", ""]
    lines += ["def compute_checkpoint_delta(items):",
              "    if not items:", "        return -1", "    return len(items)", ""]
    _write(root, {"big.py": "\n".join(lines)})
    # gold: compute_checkpoint_delta([]) returns -1


def _c_grep(root: Path) -> None:
    """A big tree; find files carrying a specific security marker."""
    _base(root)
    rng = random.Random(13)
    files = {}
    marked = set()
    for i in range(40):
        rel = f"mod/m{i:02d}.py"
        body = [f"def f{i}(x):", "    return x + 1", ""]
        if rng.random() < 0.2:  # ~20% carry the marker
            body.insert(0, "# SECURITY: validate tenant before use")
            marked.add(f"m{i:02d}.py")
        files[rel] = "\n".join(body)
    files["mod/__init__.py"] = ""
    _write(root, files)
    (root / "_gold.json").write_text(json.dumps(sorted(marked)), encoding="utf-8")


def _c_trace(root: Path) -> None:
    _base(root)
    _write(root, {
        "flow/__init__.py": "",
        "flow/api.py":
            "from flow.svc import handle\n\n"
            "def endpoint(request):\n    tenant_id = request['tenant']\n"
            "    return handle(tenant_id)\n",
        "flow/svc.py":
            "from flow.db import persist\n\n"
            "def handle(tenant_id):\n    return persist(tenant_id)\n",
        "flow/db.py":
            "def persist(tenant_id):\n    return f'saved {tenant_id}'\n",
    })
    # gold path: endpoint > handle > persist


# ============================================================= IMPLEMENT (2)
def _i_feature(root: Path) -> None:
    _base(root)
    _write(root, {
        "util/__init__.py": "",
        "util/nums.py": "def sign(x):\n    return (x > 0) - (x < 0)\n",
        # test imports clamp, which does not exist yet
        "test_nums.py":
            "from util.nums import clamp\n\n"
            "def test_clamp_low():\n    assert clamp(-5, 0, 10) == 0\n"
            "def test_clamp_high():\n    assert clamp(50, 0, 10) == 10\n"
            "def test_clamp_mid():\n    assert clamp(7, 0, 10) == 7\n",
    })


def _i_test(root: Path) -> None:
    _base(root)
    _write(root, {
        "acct/__init__.py": "",
        "acct/email.py":
            "def normalize_email(s):\n"
            "    return (s or '').strip().lower()\n",
        # no test yet; the agent must write test_email.py
    })


def _i_test_grade(root: Path, final_text: str, pytest_py: str) -> dict:
    tp = root / "test_email.py"
    if not tp.is_file():
        return {"success": False, "reason": "no test_email.py written"}
    src = tp.read_text(encoding="utf-8", errors="replace")
    calls = "normalize_email" in src
    green, line = _suite_green(root, pytest_py, "test_email.py")
    return {"success": bool(green and calls), "suite": line,
            "calls_target": calls}


# =============================================================== REFACTOR (1)
def _r_rename(root: Path) -> None:
    _base(root)
    _write(root, {
        "pkg/__init__.py": "",
        "pkg/core.py": "def oldName(x):\n    return x * 2\n",
        "pkg/use.py": "from pkg.core import oldName\n\n"
                      "def double_all(xs):\n    return [oldName(x) for x in xs]\n",
        "test_rename.py":
            "from pkg.use import double_all\n\n"
            "def test_double():\n    assert double_all([1,2]) == [2,4]\n",
    })


def _r_rename_invariant(root: Path) -> bool:
    # oldName must be gone from all .py source (the rename actually happened).
    for p in root.rglob("*.py"):
        if "oldName" in p.read_text(encoding="utf-8", errors="replace"):
            return False
    return True


# ==================================================================== DATA (1)
def _d_aggregate(root: Path) -> None:
    """A big JSONL log; count ERROR records for one service."""
    _base(root)
    rng = random.Random(17)
    services = ["auth", "billing", "search", "cache"]
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    lines = []
    gold = 0
    for _ in range(5000):
        svc = rng.choice(services)
        lvl = rng.choice(levels)
        if svc == "auth" and lvl == "ERROR":
            gold += 1
        lines.append(json.dumps({"service": svc, "level": lvl,
                                 "msg": "event " + str(rng.randint(0, 999))}))
    _write(root, {"events.jsonl": "\n".join(lines) + "\n"})
    (root / "_gold.json").write_text(json.dumps(gold), encoding="utf-8")


# --------------------------------------------------------------- answer graders
def _grade_sites_2(root, final_text, pytest_py):
    ans = _answer(final_text, "SITES")
    n = int(ans) if ans and ans.isdigit() else -1
    return {"success": n == 2, "answered": ans, "gold": 2}


def _grade_impact_files(root, final_text, pytest_py):
    got = _files_set(_answer(final_text, "FILES"))
    gold = {"server.py", "worker.py"}
    return {"success": got == gold, "answered": sorted(got), "gold": sorted(gold)}


def _grade_nav(root, final_text, pytest_py):
    ans = (_answer(final_text, "ANSWER") or "").strip()
    return {"success": ans.lstrip("=").strip() in ("-1",), "answered": ans, "gold": "-1"}


def _grade_grep_files(root, final_text, pytest_py):
    gold = set(json.loads((root / "_gold.json").read_text()))
    got = _files_set(_answer(final_text, "FILES"))
    return {"success": got == gold, "answered": sorted(got),
            "gold": sorted(gold), "n_gold": len(gold)}


def _grade_trace_path(root, final_text, pytest_py):
    ans = _answer(final_text, "PATH") or ""
    seq = [s.strip() for s in re.split(r"[>\-→,\s]+", ans) if s.strip()]
    gold = ["endpoint", "handle", "persist"]
    # ordered subsequence match (allow extra nodes between)
    it = iter(seq)
    ok = all(any(g == s for s in it) for g in gold)
    return {"success": ok, "answered": seq, "gold": gold}


def _grade_count(root, final_text, pytest_py):
    gold = int(json.loads((root / "_gold.json").read_text()))
    ans = _answer(final_text, "COUNT")
    n = int(ans) if ans and ans.lstrip("-").isdigit() else -1
    return {"success": n == gold, "answered": n, "gold": gold}


# ------------------------------------------------------------------ registry
_ANSWER = "End your final message with exactly one line: "

SCENARIOS: list[Scenario] = [
    Scenario("bug-regression", "bug", "none",
             "The test suite is failing. Find and fix the single bug so the "
             "whole suite passes. Do not weaken or delete tests.",
             _b_regression, _suite_grade(), turn_cap=20, tags=("diagnose",)),
    Scenario("bug-flood", "bug", "high",
             "The test suite is failing (it is large and noisy). Find and fix "
             "the single root-cause bug so the whole suite passes. Do not "
             "weaken or delete tests.",
             _b_flood, _suite_grade(), turn_cap=25, tags=("diagnose", "flood")),
    Scenario("bug-multi", "bug", "medium",
             "The test suite has multiple independent failures. Fix all of "
             "them so the suite passes. Do not weaken or delete tests.",
             _b_multi, _suite_grade(), turn_cap=25),
    Scenario("bug-subtle", "bug", "none",
             "A test is failing on a wrong computed value (no crash). Find and "
             "fix the bug so the suite passes. Do not weaken or delete tests.",
             _b_subtle, _suite_grade(), turn_cap=20),
    Scenario("bug-import", "bug", "none",
             "The suite fails to even run because of an import/name error. Fix "
             "it so the suite collects and passes. Do not weaken or delete "
             "tests.",
             _b_import, _suite_grade(), turn_cap=20),
    Scenario("comp-refs", "comprehension", "medium",
             "How many places is the function `charge` actually CALLED (not "
             "defined, not imported, not mentioned in a comment or string)? "
             + _ANSWER + "`SITES: <number>`",
             _c_refs, _grade_sites_2, turn_cap=18, tags=("refs",)),
    Scenario("comp-impact", "comprehension", "medium",
             "Which source files would be affected if you changed the "
             "signature of `Config.load` — i.e. which files actually CALL it? "
             + _ANSWER + "`FILES: <comma-separated filenames>`",
             _c_impact, _grade_impact_files, turn_cap=18, tags=("impact",)),
    Scenario("comp-nav", "comprehension", "high",
             "In big.py, what does the function `compute_checkpoint_delta` "
             "return when called with an empty list? "
             + _ANSWER + "`ANSWER: <value>`",
             _c_nav, _grade_nav, turn_cap=15, tags=("navigation", "flood")),
    Scenario("comp-grep", "comprehension", "high",
             "List every file under mod/ that contains a `# SECURITY:` "
             "comment (filenames only). "
             + _ANSWER + "`FILES: <comma-separated filenames>`",
             _c_grep, _grade_grep_files, turn_cap=18, tags=("search", "flood")),
    Scenario("comp-trace", "comprehension", "medium",
             "Trace how `tenant_id` flows from the HTTP endpoint to the "
             "database write: name the ordered chain of functions it passes "
             "through. " + _ANSWER + "`PATH: func1 > func2 > func3`",
             _c_trace, _grade_trace_path, turn_cap=18, tags=("trace",)),
    Scenario("impl-feature", "implement", "none",
             "A test imports a `clamp(x, lo, hi)` function that does not exist "
             "yet. Implement it so the failing tests pass. Do not change the "
             "tests.",
             _i_feature, _suite_grade(), turn_cap=18),
    Scenario("impl-test", "implement", "none",
             "The function `normalize_email` in acct/email.py has no test. "
             "Write a pytest test file `test_email.py` that calls it and "
             "covers the empty-string and whitespace cases. It must pass.",
             _i_test, _i_test_grade, turn_cap=18),
    Scenario("refactor-rename", "refactor", "medium",
             "Rename the function `oldName` to `newName` everywhere it is "
             "defined and used, keeping the suite green. No occurrence of the "
             "old name may remain in the source.",
             _r_rename, _suite_grade(_r_rename_invariant), turn_cap=20),
    Scenario("data-aggregate", "data", "high",
             "events.jsonl is a large JSON-lines log. How many records have "
             "level 'ERROR' AND service 'auth'? "
             + _ANSWER + "`COUNT: <number>`",
             _d_aggregate, _grade_count, turn_cap=15, tags=("records", "flood")),
]

BY_ID = {s.id: s for s in SCENARIOS}
