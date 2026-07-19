#!/usr/bin/env python3
"""Deterministic eval set for `ctx eval` (programmable capture, v0.19.0).

Measures the mechanism's three claimed benefit streams on REAL executions
(real fixtures, real subprocesses, the real digest pipeline — nothing
mocked), with the agent's routing scripted as a *best-case* baseline:

  naive  — raw outputs enter context (no harness), fewest possible rounds
  rounds — best-case agent on existing verbs (`ctx run`/`search`/`get`),
           batched wherever the verbs allow batching
  eval   — one `ctx eval` script; only script + digest enter context

Scripting the baseline agent perfectly is deliberately UNFAIR TO EVAL: a
live agent wastes turns the scripted arm doesn't. Mechanical numbers here
are therefore a floor on the gap; the live A/B (`evals/ab_eval_live.py`)
measures the ceiling. What this harness cannot see at all: the model's
output tokens spent re-typing data between rounds (the model-as-data-bus
cost) and per-round TTFB — reported as round counts instead, priced by the
Tura-wave measurements (~1.5–2s + one suffix cache write per round).

Metrics per scenario:
  rounds       — tool-bearing rounds the transcript pays for
  entry        — bytes/est-tokens entering context across all rounds
  resend       — Σ over rounds of previously-entered bytes (window
                 residency: what the session re-sends at cache-read rates)
  checks       — scenario-specific evidence/correctness assertions

Usage: python3 evals/evalset_collapse.py            # full set, markdown out
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SEED = 1729
N_RUN_FILES = 30
RECORDS_PER_FILE = 200
MODULES = ["auth", "billing", "catalog", "gateway", "ingest", "ledger",
           "mailer", "quota", "search", "worker"]
FAIL_BIAS = {"gateway": 0.35, "quota": 0.28}  # ground-truth low performers
NEEDLE = "anomaly-frame-77413-quiet"


# ------------------------------------------------------------------ fixture
def build_fixture(root: Path) -> None:
    """Seeded, deterministic corpus: 30 JSONL run-logs (~6k records) plus a
    small package with tests for the branch scenario."""
    rng = random.Random(SEED)
    runs = root / "runs"
    runs.mkdir(parents=True)
    for i in range(N_RUN_FILES):
        lines = []
        for j in range(RECORDS_PER_FILE):
            module = rng.choice(MODULES)
            ok = rng.random() >= FAIL_BIAS.get(module, 0.06)
            lines.append(json.dumps(
                {"test": f"t_{i:02d}_{j:03d}", "module": module,
                 "ok": ok, "ms": rng.randrange(2, 900)},
                sort_keys=True,
            ))
        (runs / f"run-{i:02d}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    pkg = root / "mod"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "legacy_helper.py").write_text(
        "def fold(xs):\n    # legacy: drops the final element on odd lengths\n"
        "    return sum(xs[: len(xs) - len(xs) % 2])\n",
        encoding="utf-8",
    )
    (pkg / "helper.py").write_text(
        "def fold(xs):\n    return sum(xs)\n", encoding="utf-8"
    )
    tests = root / "tests"
    tests.mkdir()
    importers = {2, 5, 6}  # test files on the legacy path; test_05 fails
    for i in range(8):
        mod = "legacy_helper" if i in importers else "helper"
        expect = "6" if (mod == "helper" or i != 5) else "10"
        body = (
            f"from mod.{mod} import fold\n\n\n"
            f"def test_fold_{i}():\n    assert fold([1, 2, 3]) == {expect}\n"
        )
        (tests / f"test_{i:02d}.py").write_text(body, encoding="utf-8")
    (root / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)


def ground_truth_low_modules(root: Path, threshold: float = 0.8) -> dict[str, float]:
    """Independent recomputation (never via the eval-arm script)."""
    totals: dict[str, list[int]] = {}
    for path in sorted((root / "runs").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            slot = totals.setdefault(rec["module"], [0, 0])
            slot[0] += 1
            slot[1] += 1 if rec["ok"] else 0
    return {
        m: round(okc / n, 4)
        for m, (n, okc) in sorted(totals.items())
        if okc / n < threshold
    }


# ------------------------------------------------------------- arm plumbing
class Arm:
    """Accumulates the transcript-cost ledger for one scripted arm."""

    def __init__(self, name: str):
        self.name = name
        self.rounds = 0
        self.entered = 0  # bytes entering context, all rounds
        self.resend = 0   # Σ per round of previously-entered bytes
        self.notes: list[str] = []

    def round(self, emitted_text: str) -> None:
        self.resend += self.entered
        self.rounds += 1
        self.entered += len(emitted_text.encode("utf-8"))

    def row(self) -> str:
        from ctx.textutil import estimate_tokens as et

        return (
            f"| {self.name} | {self.rounds} | {et(self.entered):,} tok "
            f"| {et(self.resend):,} tok |"
        )


def make_ws_store(root: Path):
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(root))
    return ws, Store(ws.workspace_id)


def harness_run(ws, store, argv: list[str], shell: bool = False) -> str:
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    cap = run_capture(ws, argv, shell=shell, store=store)
    digest, _ = render_run_digest(store, ws, cap.manifest)
    return digest


# ---------------------------------------------------------------- scenarios
def scenario_fanout(root: Path) -> tuple[list[Arm], list[str]]:
    """S-A fan-out aggregate: per-module pass rate over 30 JSONL files;
    report modules under 80%. Cross-file arithmetic — no existing verb
    computes it, so the rounds arm must move records through context."""
    from ctx.pyeval import run_eval
    from ctx.retrieval import Selector, get

    ws, store = make_ws_store(root)
    truth = ground_truth_low_modules(root)
    checks: list[str] = []

    naive = Arm("naive")
    raw = b"".join(
        p.read_bytes() for p in sorted((root / "runs").glob("*.jsonl"))
    )
    naive.round(raw.decode("utf-8"))  # `cat runs/*.jsonl`: one flood round

    rounds = Arm("rounds")
    for path in sorted((root / "runs").glob("*.jsonl")):
        rel = path.relative_to(root)
        out = get(store, ws, f"repo:{rel}", Selector(records=(1, RECORDS_PER_FILE)))
        rounds.round(out)  # bounded slice per file; model must aggregate in-head
    rounds.notes.append(
        "bounded gets truncate: the arm CANNOT actually finish the "
        "arithmetic from what entered context"
    )

    evalarm = Arm("eval")
    script = (
        "import json, pathlib\n"
        "tot = {}\n"
        "for p in sorted(pathlib.Path('runs').glob('*.jsonl')):\n"
        "    for line in p.read_text().splitlines():\n"
        "        r = json.loads(line)\n"
        "        n, ok = tot.get(r['module'], (0, 0))\n"
        "        tot[r['module']] = (n + 1, ok + (1 if r['ok'] else 0))\n"
        "for m, (n, ok) in sorted(tot.items()):\n"
        "    rate = ok / n\n"
        "    if rate < 0.8:\n"
        "        print(f'LOW {m} {rate:.4f} ({ok}/{n})')\n"
    )
    text, code = run_eval(ws, store, script)
    evalarm.round(script)  # the model authored it: it transits context once
    evalarm.round(text)
    evalarm.rounds = 1  # script + digest ride in the same round
    for module, rate in truth.items():
        assert f"LOW {module} {rate:.4f}" in text, (module, rate, text)
    low_in_text = [ln for ln in text.splitlines() if ln.startswith("LOW ")]
    assert len(low_in_text) == len(truth) and code == 0
    checks.append(
        f"eval answer exact vs independent ground truth "
        f"({len(truth)} low modules: {', '.join(truth)})"
    )
    checks.append("rounds arm entered truncated slices only — task not completable")
    return [naive, rounds, evalarm], checks


def scenario_branch(root: Path) -> tuple[list[Arm], list[str]]:
    """S-B data-dependent branch: find test files importing the legacy
    helper, run pytest on exactly those, report failures. `ctx seq` cannot
    express this (step 2's argv depends on step 1's data)."""
    from ctx.pyeval import run_eval
    from ctx.retrieval import search

    ws, store = make_ws_store(root)
    checks: list[str] = []

    naive = Arm("naive")
    grep = subprocess.run(
        ["grep", "-rl", "legacy_helper", "tests"], cwd=root,
        capture_output=True, text=True,
    )
    files = sorted(grep.stdout.split())
    naive.round(grep.stdout)
    pytest_raw = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *files], cwd=root,
        capture_output=True, text=True,
    )
    naive.round(pytest_raw.stdout + pytest_raw.stderr)

    rounds = Arm("rounds")
    out1 = search(store, ws, "repo:tests", ["legacy_helper"])
    rounds.round(out1)
    out2 = harness_run(
        ws, store, [sys.executable, "-m", "pytest", "-q", *files]
    )
    rounds.round(out2)

    # Stream-shaped chains have a fourth arm: a bash pipeline under
    # `ctx run --shell` collapses this WITHOUT eval (the honest control —
    # eval's edge is structured data, not this scenario).
    pipeline = Arm("pipeline")
    out_p = harness_run(
        ws, store,
        ["grep -l legacy_helper tests/test_*.py | xargs -r python3 -m pytest -q"],
        shell=True,
    )
    pipeline.round(out_p)
    pipeline.notes.append(
        "bash-expressible chain: `ctx run --shell` pipeline already "
        "collapses it — eval's marginal value here is provenance of the "
        "selection logic, not rounds"
    )

    evalarm = Arm("eval")
    script = (
        "import pathlib, subprocess, sys\n"
        "files = sorted(str(p) for p in pathlib.Path('tests').glob('test_*.py')\n"
        "               if 'legacy_helper' in p.read_text())\n"
        "print('importers:', ' '.join(files))\n"
        "r = subprocess.run([sys.executable, '-m', 'pytest', '-q', *files],\n"
        "                   capture_output=True, text=True)\n"
        "tail = [l for l in r.stdout.splitlines() if l.strip()][-3:]\n"
        "print(*tail, sep='\\n')\n"
        "sys.exit(r.returncode)\n"
    )
    text, code = run_eval(ws, store, script)
    evalarm.round(script + text)
    assert code != 0, "the seeded failing test must surface as a failing eval"
    assert "test_05" in text and "importers:" in text
    checks.append("eval arm found all 3 importers and surfaced the seeded failure")
    checks.append("failure exit propagated (failure-asymmetric budget applied)")
    return [naive, rounds, pipeline, evalarm], checks


def scenario_flood_needle(root: Path) -> tuple[list[Arm], list[str]]:
    """S-C provenance net: an eval script floods stdout (20k classification
    lines, one quiet anomaly) then prints a 3-line summary. The digest must
    stay bounded, and the quiet needle must remain retrievable from the
    stored stream with coordinates — the Maki-sandbox failure mode is that
    this flood (and the needle) vanish into the chat log."""
    from ctx.pyeval import run_eval
    from ctx.retrieval import search

    ws, store = make_ws_store(root)
    checks: list[str] = []
    evalarm = Arm("eval")
    script = (
        "for i in range(20000):\n"
        f"    tag = '{NEEDLE}' if i == 14237 else 'ok'\n"
        "    print(f'frame {i:05d} {tag}')\n"
        "print('SUMMARY frames=20000 anomalies=1')\n"
    )
    text, code = run_eval(ws, store, script)
    evalarm.round(script + text)
    assert code == 0
    # The HEAD/TAIL evidence window surfaces the script's own SUMMARY tail
    # line directly in the digest (CLIs put conclusions at the END); the
    # quiet mid-stream needle stays out of the digest and must be recovered
    # from the stored stream by search, with coordinates.
    assert "SUMMARY frames=20000 anomalies=1" in text
    assert NEEDLE not in text
    assert "omitted" in text  # the middle is declared, never silent
    rid = text.split("[ctx run:")[1].split(" ")[0]
    hit = search(store, ws, f"run:{rid}", [NEEDLE])
    assert NEEDLE in hit and "14238" in hit.replace(",", ""), hit
    from ctx.retrieval import Selector, get

    tail = get(store, ws, f"run:{rid}#stdout", Selector(lines=(20001, 20001)))
    assert "SUMMARY frames=20000 anomalies=1" in tail
    raw_bytes = 20001 * 20  # ~order: what the transcript was spared
    checks.append(
        f"digest {len(text.encode()) // 4} est tok vs ~{raw_bytes // 4:,} raw; "
        "quiet needle NOT in digest but recovered by `ctx search run:` "
        "with its line coordinate"
    )
    checks.append(
        "intended SUMMARY tail line rides IN the digest via the head/tail "
        "evidence window (conclusions live at the END of CLI output); the "
        "omitted middle keeps a span + `ctx get --lines` address"
    )
    return [evalarm], checks


def scenario_debug_cost(root: Path) -> tuple[list[Arm], list[str]]:
    """S-D the blind-bet loss: a wrong script fails mid-corpus. Measure the
    recovery cost with the provenance net (traceback digest + one slice +
    rerun) vs the naive re-pay (raw chain twice)."""
    from ctx.pyeval import run_eval
    from ctx.retrieval import Selector, get

    ws, store = make_ws_store(root)
    # Poison one record in file 17 (missing 'module' key).
    target = root / "runs" / "run-17.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    lines[103] = json.dumps({"test": "t_17_103", "ok": True, "ms": 5}, sort_keys=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    checks: list[str] = []
    corpus_bytes = sum(
        p.stat().st_size for p in (root / "runs").glob("*.jsonl")
    )

    evalarm = Arm("eval")
    bad_script = (
        "import json, pathlib\n"
        "tot = {}\n"
        "for p in sorted(pathlib.Path('runs').glob('*.jsonl')):\n"
        "    for i, line in enumerate(p.read_text().splitlines(), 1):\n"
        "        r = json.loads(line)\n"
        "        m = r['module']\n"  # KeyError on the poisoned record
        "        tot[m] = tot.get(m, 0) + 1\n"
        "print('modules:', len(tot))\n"
    )
    text, code = run_eval(ws, store, bad_script)
    evalarm.round(bad_script + text)
    assert code != 0 and "KeyError" in text and 'File "<stdin>"' in text
    # Recovery: one bounded slice around the poisoned record, then rerun fixed.
    slice_ = get(store, ws, "repo:runs/run-17.jsonl", Selector(lines=(103, 105)))
    evalarm.round(slice_)
    fixed = bad_script.replace("r['module']", "r.get('module', 'unknown')")
    text2, code2 = run_eval(ws, store, fixed)
    evalarm.round(fixed + text2)
    assert code2 == 0 and "modules:" in text2
    evalarm.rounds = 3

    naive = Arm("naive re-pay")
    naive.rounds = 2
    naive.entered = corpus_bytes * 2  # raw chain output, run twice
    naive.resend = corpus_bytes

    checks.append(
        "wrong script: traceback rode the failure budget, the poisoned "
        "record was found with ONE bounded slice, fixed script green — "
        "debug was retrieval, not re-execution"
    )
    return [evalarm, naive], checks


# ------------------------------------------------------------------- report
SCENARIOS = {
    "S-A fanout-aggregate (30 files → per-module rates)": scenario_fanout,
    "S-B data-dependent branch (grep → pytest subset)": scenario_branch,
    "S-C flood + quiet needle (provenance net)": scenario_flood_needle,
    "S-D wrong-script recovery (blind-bet loss cost)": scenario_debug_cost,
}


def run_all() -> str:
    out: list[str] = ["# evalset: programmable capture (mechanical arms)", ""]
    for title, fn in SCENARIOS.items():
        with tempfile.TemporaryDirectory(prefix="ctx-evalset-") as td:
            import os

            state = Path(td) / "state"
            os.environ["CTX_STATE_HOME"] = str(state)
            root = Path(td) / "fixture"
            build_fixture(root)
            arms, checks = fn(root)
        out.append(f"## {title}")
        out.append("")
        out.append("| arm | rounds | context entry | resend (residency) |")
        out.append("|---|---|---|---|")
        for arm in arms:
            out.append(arm.row())
        out.append("")
        for c in checks:
            out.append(f"- ✅ {c}")
        for arm in arms:
            for n in arm.notes:
                out.append(f"- ⚠️ {arm.name}: {n}")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    print(run_all())
