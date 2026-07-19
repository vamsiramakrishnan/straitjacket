#!/usr/bin/env python3
"""ALGEBRA n>=3 taught-vs-untaught referee (the live-A/B receipt, codified).

Codifies the flow behind the "ALGEBRA live A/B" addendum in
evals/spec3-haiku-2026-07-18.md: the n=1 honest negative (untaught haiku
beat the taught arm; the model ran the join 3x dry before any captured run
existed) requires an n>=3 paired referee before ANY doctrine claim — the
round-3 variance-wall lesson applied to the algebra.

Design:
- Fixture: a git repo with 3 src modules + tests. The BASE COMMIT is green
  EXCEPT one pre-existing failure (the gamma median index bug, committed).
  UNCOMMITTED working-tree edits introduce 2 exception-raising bugs whose
  frames land in src/ (the receipt's requirement — the fails|in-changed
  join locates frames, so introduced failures must raise inside changed
  src files, not assert in test bodies).
- Arms differ ONLY in the teach line: taught = `ctx wrap claude` +
  `--append-system-prompt` two-command protocol; untaught = `ctx wrap
  claude` only. Same model, same fixture, same prompt, same tools, same
  25-turn cap.
- Grading is mechanical: (a) format — the two required output lines
  present and matching ground truth (INTRODUCED={test_add,test_scale},
  PREEXISTING={test_median}); (b) fixes — post-hoc pytest in the fixture
  shows EXACTLY the gamma failure remaining; (c) economics — turns, cost,
  wall-clock, cache hit.
- Aggregation REUSES evals/spec3_runner.py's medians machinery
  (`aggregate_rows`) unchanged; --repeats N gives per-rep isolated
  fixtures + CLAUDE_CONFIG_DIRs; --gates evaluates the doctrine bar on
  medians/fractions and exits 0/1.

Usage:
    python3 evals/ab_algebra_live.py --out /tmp/ab-algebra --repeats 3 --gates
        [--model haiku]

Live sessions cost money — the integrator runs this. Unit coverage
(grading, aggregation, gates, fixture ground truth, frozen constants)
lives in tests/test_reflex.py and never launches a session.

Requires: `claude` CLI on PATH with credentials, `ctx` installed
(`pip install -e .`).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from spec3_runner import aggregate_rows, parse_result_json  # frozen, unmodified

MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": None}
TOOLS = "Bash Read Grep Glob Edit Write"
MAX_TURNS = 25
SESSION_TIMEOUT = 1500
TASK_NAME = "algebra_classify"

# --------------------------------------------------------------------------
# FROZEN REFEREE (the spec3 pattern, debt 34e21fe2dc lineage): TASK_PROMPT,
# TEACH, BASE_FILES, INTRODUCED_EDITS, ground truth, and arm construction
# (arm_argv, incl. MODELS/TOOLS/MAX_TURNS) are the frozen n>=3 referee for
# the algebra — cross-round comparisons die if one byte changes. Guarded by
# tests/test_reflex.py::test_ab_algebra_frozen_referee_constants (sha256
# over these constants asserted against a recorded value). The prompt and
# teach text recreate the live-A/B receipt flow (the scratchpad original is
# gone; evals/spec3-haiku-2026-07-18.md's ALGEBRA addendum is the source).
# --------------------------------------------------------------------------
TASK_PROMPT = (
    "This repository's pytest suite has failing tests. Some failures are "
    "INTRODUCED by the uncommitted working-tree edits; the rest are "
    "PREEXISTING (they fail at the committed base too). Classify every "
    "failing test, then fix ONLY the introduced failures (leave "
    "pre-existing behavior untouched; do not run git commit). Finish with "
    "EXACTLY these two lines:\n"
    "INTRODUCED: <comma-separated test names>\n"
    "PREEXISTING: <comma-separated test names>"
)

# The two-command protocol (the teach whose step 1 the n=1 taught arm
# skipped — precondition opacity, the receipt's diagnosis).
TEACH = (
    "ctx is installed. Two-command protocol for classifying failures: "
    "STEP 1 — capture a test run under the birth gate first: "
    "ctx run -- python -m pytest tests -q. "
    "STEP 2 — join the captured failures against the uncommitted change "
    "set: ctx q 'fails last | in-changed'. Failing tests returned by the "
    "join are INTRODUCED by the working-tree edits; captured failing "
    "tests absent from the join are PREEXISTING. Step 2 is dry without "
    "step 1: run the capture before the join."
)

BASE_FILES = {
    "ctx.toml": "version = 1\n",
    "src/__init__.py": "",
    "src/alpha.py": textwrap.dedent('''\
        """Alpha module (committed healthy)."""


        def add(a, b):
            return a + b
    '''),
    "src/beta.py": textwrap.dedent('''\
        """Beta module (committed healthy)."""


        def scale(values, factor):
            return [v * factor for v in values]
    '''),
    "src/gamma.py": textwrap.dedent('''\
        """Gamma module: the PRE-EXISTING median index bug (committed).

        Even-length inputs should average the two middle values; this
        indexes the upper middle instead — test_median fails at the base
        commit, before any working-tree edit."""


        def median(values):
            ordered = sorted(values)
            return ordered[len(ordered) // 2]
    '''),
    "tests/conftest.py": (
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
    ),
    "tests/test_suite.py": textwrap.dedent('''\
        from src.alpha import add
        from src.beta import scale
        from src.gamma import median


        def test_add():
            assert add(2, 3) == 5


        def test_scale():
            assert scale([1, 2], 3) == [3, 6]


        def test_median():
            assert median([1, 2, 3, 4]) == 2.5


        def test_ballast_strings():
            assert "ctx".upper() == "CTX"


        def test_ballast_lists():
            assert sorted([3, 1, 2]) == [1, 2, 3]
    '''),
}

# Uncommitted working-tree edits: two exception-raising bugs whose frames
# land in src/ (the join's locating requirement).
INTRODUCED_EDITS = {
    "src/alpha.py": textwrap.dedent('''\
        """Alpha module: seeded INTRODUCED failure #1."""


        def add(a, b):
            staged = [a, b]
            total = sum(staged)
            marker = "alpha add %d" % total
            checked = marker.upper()
            assert checked
            raise ValueError("introduced: add regression (" + checked + ")")
    '''),
    "src/beta.py": textwrap.dedent('''\
        """Beta module: seeded INTRODUCED failure #2."""


        def scale(values, factor):
            table = {"values": list(values), "factor": factor}
            keys = sorted(table)
            audit = ["%s=%s" % (k, table[k]) for k in keys]
            joined = ", ".join(audit)
            assert joined
            return table["missing-key"]
    '''),
}

INTRODUCED_TESTS = ("test_add", "test_scale")
PREEXISTING_TESTS = ("test_median",)


def arm_argv(arm: str, model: str) -> list[str]:
    """Frozen arm construction: both arms are `ctx wrap claude`; the ONLY
    delta is the appended teach line."""
    base = ["claude", "-p", TASK_PROMPT, "--max-turns", str(MAX_TURNS),
            "--output-format", "json", "--allowedTools", TOOLS]
    if MODELS[model]:
        base += ["--model", MODELS[model]]
    if arm == "taught":
        base += ["--append-system-prompt", TEACH]
    elif arm != "untaught":
        raise ValueError(arm)
    return ["ctx", "wrap", "claude", "--"] + base[1:]


# ------------------------------------------------------------------ fixture
def make_fixture(dest: pathlib.Path, *, introduced: bool = True) -> None:
    """Seed the frozen fixture: base commit (green except gamma), then —
    unless ``introduced=False`` (ground-truth verification of the base) —
    the uncommitted exception-raising edits."""
    dest.mkdir(parents=True)
    for rel, text in sorted(BASE_FILES.items()):
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "base"],
        cwd=dest, check=True,
    )
    if introduced:
        for rel, text in sorted(INTRODUCED_EDITS.items()):
            (dest / rel).write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ grading
_NAME_SPLIT_RE = re.compile(r"[,\s]+")


def _line_set(text: str, label: str) -> set[str] | None:
    """Test-name set from the LAST ``<label>: ...`` line, or None when the
    line is absent. Names tolerate node-id spellings (``tests/x.py::t``),
    backticks, and trailing punctuation."""
    matches = re.findall(rf"^\s*{label}:\s*(.*?)\s*$", text or "", re.M)
    if not matches:
        return None
    names: set[str] = set()
    for raw in _NAME_SPLIT_RE.split(matches[-1]):
        name = raw.strip().strip("`'\".,;:")
        if "::" in name:
            name = name.split("::")[-1]
        if name and name.lower() not in ("none", "-"):
            names.add(name)
    return names


def grade_format(result_text: str) -> dict:
    """Mechanical format grade against ground truth: both required lines
    present, and both name sets exactly right."""
    intro = _line_set(result_text or "", "INTRODUCED")
    pre = _line_set(result_text or "", "PREEXISTING")
    present = intro is not None and pre is not None
    correct = bool(
        present
        and intro == set(INTRODUCED_TESTS)
        and pre == set(PREEXISTING_TESTS)
    )
    return {
        "format_present": present,
        "format_correct": correct,
        "claimed_introduced": sorted(intro or []),
        "claimed_preexisting": sorted(pre or []),
    }


def failing_tests(pytest_output: str) -> set[str]:
    """Failing/erroring test names from a ``pytest -q -rf`` run."""
    out = pytest_output or ""
    names = set()
    for nodeid in re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", out, re.M):
        names.add(nodeid.split("::")[-1].split(" ")[0])
    return names


def fixes_correct(failing: set[str]) -> bool:
    """The fix grade: EXACTLY the pre-existing gamma failure remains —
    introduced failures fixed, pre-existing behavior untouched."""
    return failing == set(PREEXISTING_TESTS)


def grade_fixes(fixture: pathlib.Path) -> dict:
    """Post-hoc pytest in the finished fixture (never seen by the agent as
    a grading step)."""
    proc = subprocess.run(
        ["python3", "-m", "pytest", "tests", "-q", "-rf", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=fixture, capture_output=True, text=True, timeout=180,
    )
    failing = failing_tests(proc.stdout + proc.stderr)
    return {
        "still_failing": sorted(failing),
        "fixes_correct": fixes_correct(failing),
    }


# ---------------------------------------------------------------- sessions
def run_arm(arm: str, model: str, rep_out: pathlib.Path) -> dict:
    """One live session: isolated fixture + CLAUDE_CONFIG_DIR, then
    mechanical grading. The integrator runs this — tests never do."""
    fixture = rep_out / f"fixture-{arm}"
    if fixture.exists():
        shutil.rmtree(fixture)
    make_fixture(fixture)
    cfg = rep_out / f"cc-{arm}"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg),
           "PIP_REQUIRE_VIRTUALENV": "1"}
    t0 = time.monotonic()
    proc = subprocess.run(
        arm_argv(arm, model), cwd=fixture, env=env,
        capture_output=True, text=True, timeout=SESSION_TIMEOUT,
    )
    wall = round(time.monotonic() - t0, 1)
    (rep_out / f"{arm}.raw").write_text(proc.stdout, encoding="utf-8")
    (rep_out / f"{arm}.err").write_text(proc.stderr, encoding="utf-8")
    doc = parse_result_json(proc.stdout)
    u = doc.get("usage", {})
    reads = u.get("cache_read_input_tokens") or 0
    writes = u.get("cache_creation_input_tokens") or 0
    uncached = u.get("input_tokens") or 0
    denom = reads + writes + uncached
    row = {
        "task": TASK_NAME, "arm": arm,
        "turns": doc.get("num_turns"),
        "cost_usd": round(doc.get("total_cost_usd") or 0.0, 4),
        "api_duration_s": round((doc.get("duration_ms") or 0) / 1000, 1),
        "wall_s": wall,
        "cache_hit_pct": round(100 * reads / denom, 1) if denom else None,
        "output_tokens": u.get("output_tokens"),
        "session_error": (doc == {}),
    }
    row.update(grade_format(doc.get("result") or ""))
    row.update(grade_fixes(fixture))
    row["correct"] = bool(row["format_correct"] and row["fixes_correct"])
    return row


# ------------------------------------------------------------------- gates
def _fraction(rows: list[dict], arm: str, key: str) -> float | None:
    """Fraction of the arm's reps with truthy ``key``. Failed sessions
    count in the denominator as not-correct (a session that died is not a
    format success). None when the arm has no rows (gates FAIL closed)."""
    arm_rows = [r for r in rows if r.get("arm") == arm]
    if not arm_rows:
        return None
    return sum(1 for r in arm_rows if r.get(key)) / len(arm_rows)


def evaluate_ab_gates(rows: list[dict], medians: dict) -> tuple[list[dict], bool]:
    """The doctrine bar (frozen BEFORE any n>=3 round runs, the EDC §19.2
    discipline): the teach must not cost turns AND must not cost format
    discipline —

    - turns: taught median turns <= untaught median turns
    - format: taught format-correct fraction >= untaught format-correct
      fraction

    Missing inputs FAIL closed. Pure — unit-testable with synthetic rows."""
    gates: list[dict] = []

    def _med(arm: str):
        return medians.get(f"{TASK_NAME}/{arm}", {}).get("turns", {}).get("median")

    t_med, u_med = _med("taught"), _med("untaught")
    if t_med is None or u_med is None:
        gates.append({"gate": "taught_turns<=untaught_turns", "ok": False,
                      "detail": "missing taught/untaught turn medians (FAIL closed)"})
    else:
        gates.append({
            "gate": "taught_turns<=untaught_turns", "ok": t_med <= u_med,
            "detail": f"taught median {t_med:g} vs untaught median {u_med:g}",
        })
    t_fmt = _fraction(rows, "taught", "format_correct")
    u_fmt = _fraction(rows, "untaught", "format_correct")
    if t_fmt is None or u_fmt is None:
        gates.append({"gate": "taught_format>=untaught_format", "ok": False,
                      "detail": "missing taught/untaught format fractions (FAIL closed)"})
    else:
        gates.append({
            "gate": "taught_format>=untaught_format", "ok": t_fmt >= u_fmt,
            "detail": (f"taught format-correct {t_fmt:.2f} vs "
                       f"untaught {u_fmt:.2f}"),
        })
    return gates, all(g["ok"] for g in gates)


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="haiku", choices=list(MODELS))
    ap.add_argument("--repeats", type=int, default=1,
                    help="paired seeds per arm (n>=3 before any doctrine claim)")
    ap.add_argument("--gates", action="store_true",
                    help="evaluate the taught-vs-untaught doctrine gates on "
                         "medians/fractions; exit 1 on any FAIL")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    import concurrent.futures

    rows: list[dict] = []
    # Reps sequential (cost control); the two arms concurrent within a rep
    # — fully isolated fixtures and config dirs (the spec3 pattern).
    for rep in range(1, args.repeats + 1):
        rep_out = args.out if args.repeats == 1 else args.out / f"rep{rep}"
        rep_out.mkdir(parents=True, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = {pool.submit(run_arm, arm, args.model, rep_out): arm
                    for arm in ("taught", "untaught")}
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result()
                r["rep"] = rep
                rows.append(r)
                print(f"session done: rep{rep} {r['arm']} · turns={r['turns']} "
                      f"cost=${r['cost_usd']} format={r['format_correct']} "
                      f"fixes={r['fixes_correct']}", flush=True)

    rows.sort(key=lambda r: (0 if r["arm"] == "taught" else 1, r["rep"]))
    medians = aggregate_rows(rows)  # REUSED from the frozen spec3 runner
    gates, gates_ok = evaluate_ab_gates(rows, medians)
    summary = {"schema": "ab_algebra.summary/v1", "repeats": args.repeats,
               "rows": rows, "medians": medians, "gates": gates,
               "gates_ok": gates_ok}
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    cols = ["rep", "arm", "turns", "cost_usd", "wall_s", "cache_hit_pct",
            "format_correct", "fixes_correct", "correct"]
    print("\n| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    print("\nmedians per arm (median [min-max] across reps):")
    for key, b in medians.items():
        frag = []
        for metric, fmt in (("turns", "turns {med:g} [{lo:g}-{hi:g}]"),
                            ("cost_usd", "cost ${med:g} [{lo:g}-{hi:g}]"),
                            ("wall_s", "wall {med:g}s [{lo:g}-{hi:g}]"),
                            ("cache_hit_pct", "cache {med:g}% [{lo:g}-{hi:g}]")):
            m = b.get(metric)
            if m:
                frag.append(fmt.format(med=m["median"], lo=m["min"], hi=m["max"]))
        print(f"  {key}: n={b['n']} · " + " · ".join(frag))
    print("\ntaught-vs-untaught doctrine gates:")
    for g in gates:
        print(f"  {g['gate']}: {'PASS' if g['ok'] else 'FAIL'} — {g['detail']}")
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")
    print("\nAB_ALGEBRA_DONE", flush=True)
    return 0 if (gates_ok or not args.gates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
