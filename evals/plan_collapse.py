#!/usr/bin/env python3
"""Compiled evidence plans: cost/turn/cache evidence (docs/EVIDENCE-PLANS.md).

Measures the transcript economics of one seeded auth-regression diagnosis
across three arms, on REAL bytes — real git, real pytest, the real digest
and plan-executor pipelines. Nothing here is a modeled constant: every
number the doc cites is a byte count this script produced.

  ARM N  naive interactive — a bare agent's canonical exploration sequence
         (git status/diff, pytest, grep, cat×2); raw command output enters
         context, one model round per command.
  ARM B  harnessed interactive — the same epistemic steps through the
         shipped verbs (`ctx run`, `ctx search`, `ctx get`); only the
         bounded digest bytes enter context, still one round per step.
  ARM P  compiled plan — the 5-node diagnosis DAG through `ctx plan run`;
         one boundary crossing, one investigation digest, plus the
         model-authored plan JSON (counted as OUTPUT, not input).

What this harness CANNOT see (declared, so the doc does not overclaim):
there is no live model loop here — no turns-to-fix, no answer quality, no
per-round TTFB or cache-write wire cost. Those need the four-arm live
referee (docs/EVIDENCE-PLANS.md), which this environment cannot run
(Headroom's proxy is not installed). This is transcript byte-flow only.

Usage: python3 evals/plan_collapse.py     # rebuilds the fixture, prints table
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import random
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SEED = 1729
NOISE_FUNCS = 60  # ~300 lines of realistically-noisy source

# The 5-node diagnosis plan the model authors once (docs/EVIDENCE-PLANS.md).
DIAGNOSIS_PLAN = {
    "version": "ctx.plan/v1",
    "objective": {"kind": "diagnose",
                  "question": "which changed symbols explain the failures?"},
    "budget": {"wall_seconds": 120},
    "steps": [
        {"id": "changes", "op": "repo.changed"},
        {"id": "tests", "op": "test.run",
         # sys.executable, not a bare python3 — parity with the naive and
         # harness arms below, and the only interpreter guaranteed to
         # carry pytest in any environment running this eval.
         "args": {"command": f"{shlex.quote(sys.executable)} -m pytest -q"}},
        {"id": "culprits", "op": "evidence.join",
         "args": {"on": "failing_in_changed"}, "after": ["tests", "changes"]},
        {"id": "counter", "op": "evidence.join",
         "args": {"on": "untouched_failures"}, "after": ["tests"]},
        {"id": "probe", "op": "ast.search",
         "args": {"pattern": "from_request($ARG)"}, "when": "culprits.count > 0"},
    ],
}


# ------------------------------------------------------------------ fixture
def build_fixture(root: Path) -> None:
    """A committed baseline (auth.py + test_auth.py + a ~300-line noise
    module), then a regression edit that adds `raise ValueError('missing
    tenant')` inside from_request. Fixture shape mirrors
    tests/test_plan_exec.py::seeded_repo; caches are gitignored so the
    worktree state is stable across executions."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n",
                                     encoding="utf-8")
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "auth.py").write_text(
        "def normalize_tenant(t):\n"
        "    return (t or '').strip().lower()\n"
        "\n"
        "def from_request(tenant_id):\n"
        "    return {'tenant': tenant_id}\n",
        encoding="utf-8",
    )
    (root / "test_auth.py").write_text(
        "from auth import from_request\n"
        "\n"
        "def test_tenant_none():\n"
        "    assert from_request(None)['tenant'] == ''\n",
        encoding="utf-8",
    )
    # Noise module: unrelated helpers whose comments mention from_request in a
    # scattered handful of lines, so a raw grep is realistically noisy while a
    # structural search stays precise.
    rng = random.Random(SEED)
    lines: list[str] = []
    for i in range(NOISE_FUNCS):
        lines.append(f"def noise_{i:03d}(a, b):")
        if i % 17 == 3:
            lines.append("    # see from_request for the tenant convention")
        lines.append(f"    total = a + b + {rng.randrange(100)}")
        lines.append("    return total")
        lines.append("")
    (root / "noise.py").write_text("\n".join(lines), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, env=env)
    # The regression: a raise inside the CHANGED file, so the failure's
    # deepest frame lands in changed source (the root-cause join's shape).
    (root / "auth.py").write_text(
        "def normalize_tenant(t):\n"
        "    return (t or '').strip().lower()\n"
        "\n"
        "def from_request(tenant_id):\n"
        "    if tenant_id is None:\n"
        "        raise ValueError('missing tenant')\n"
        "    return {'tenant': tenant_id}\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------- arm plumbing
def _raw(root: Path, argv: list[str]) -> str:
    r = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    return r.stdout + r.stderr


def _ctx(root: Path, argv: list[str]) -> str:
    from ctx.cli import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["--workspace", str(root)] + argv)
    return buf.getvalue()


def arm_naive(root: Path) -> list[tuple[str, str]]:
    """Raw command output — one model round each, exactly what a bare agent
    reads: status, diff, the full pytest transcript, a noisy grep, two cats."""
    return [
        ("git status --porcelain", _raw(root, ["git", "status", "--porcelain"])),
        ("git diff HEAD", _raw(root, ["git", "diff", "HEAD"])),
        ("pytest -q", _raw(root, [sys.executable, "-m", "pytest", "-q"])),
        ("grep -rn from_request .", _raw(root, ["grep", "-rn", "from_request", "."])),
        ("cat auth.py", (root / "auth.py").read_text(encoding="utf-8")),
        ("cat test_auth.py", (root / "test_auth.py").read_text(encoding="utf-8")),
    ]


def arm_harness(root: Path) -> list[tuple[str, str]]:
    """The same epistemic steps through shipped verbs — bounded digests only."""
    return [
        ("ctx run -- git diff HEAD", _ctx(root, ["run", "--", "git", "diff", "HEAD"])),
        ("ctx run -- pytest -q",
         _ctx(root, ["run", "--", sys.executable, "-m", "pytest", "-q"])),
        ("ctx search repo: from_request", _ctx(root, ["search", "repo:", "from_request"])),
        ("ctx get repo:auth.py --symbol from_request",
         _ctx(root, ["get", "repo:auth.py", "--symbol", "from_request"])),
    ]


def arm_plan(root: Path) -> tuple[list[tuple[str, str]], str]:
    """One compiled plan → one digest. The plan JSON is model OUTPUT."""
    plan_json = json.dumps(DIAGNOSIS_PLAN)
    (root / "plan.json").write_text(plan_json, encoding="utf-8")
    digest = _ctx(root, ["plan", "run", "plan.json"])
    return [("ctx plan run (5-node diagnosis)", digest)], plan_json


# ------------------------------------------------------------ cost + cache
def nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def est_tokens(n: int) -> int:
    from ctx.textutil import estimate_tokens

    return estimate_tokens(n)


def first_exposure(steps: list[tuple[str, str]]) -> int:
    """Σ O_i — bytes crossing the boundary once, at first exposure."""
    return sum(nbytes(s) for _, s in steps)


def resend_cost(steps: list[tuple[str, str]]) -> int:
    """C = Σ_i (i · O_i) — latency-weighted context cost under the append-
    only resend model: an output that only arrives at round i drags the
    whole accumulated prefix to get there, so it is weighted by the round
    index at which it finally lands. One early crossing beats six late ones
    even at equal bytes. R=1 arms therefore pay exactly O_1."""
    return sum(i * nbytes(s) for i, (_, s) in enumerate(steps, start=1))


_HEX = re.compile(r"[0-9a-f]{8,}")
_TIMING = re.compile(r" in \d+\.\d+s")
_DENSIFY = "densified: re-run detected · full evidence inline\n"


def _normalize(s: str) -> str:
    """Mask the volatile tokens so the *evidence* bytes can be compared:
    per-capture artifact handles (run:/investigate:/snapshot: hex), pytest
    wall-clock timing, and the re-run densification banner."""
    return _TIMING.sub(" in Ns", _HEX.sub("HEX", s.replace(_DENSIFY, "")))


def cache_probe(build, run_steps) -> list[tuple[str, bool, str]]:
    """Run the arm's steps twice on the same unchanged worktree + store and
    diff each step. byte-identical => a cache-aligned append. Otherwise the
    cause is classified from the diff (never guessed)."""
    with tempfile.TemporaryDirectory(prefix="ctx-plancol-cache-") as td:
        os.environ["CTX_STATE_HOME"] = str(Path(td) / "state")
        root = Path(td) / "proj"
        root.mkdir()
        build(root)
        first = run_steps(root)
        second = run_steps(root)
    if isinstance(first, tuple):
        first, second = first[0], second[0]
    out: list[tuple[str, bool, str]] = []
    for (label, a), (_, b) in zip(first, second):
        if a == b:
            # Byte-stable this sampling. Naive raw pytest still carries a
            # wall-clock `in N.NNs` token that is volatile by construction —
            # two adjacent runs can coincide, but the token is not stable.
            note = ("—" if not _TIMING.search(a)
                    else "carries volatile ` in N.NNs` (sample coincided)")
            out.append((label, True, note))
            continue
        norm_eq = _normalize(a) == _normalize(b)
        if _TIMING.search(a) and _TIMING.sub("", a) == _TIMING.sub("", b):
            cause = "pytest wall-clock ` in N.NNs`"
        elif (" dense" in b) != (" dense" in a):
            cause = "re-run densification (denser digest) + capture-id"
        elif _DENSIFY in b or _DENSIFY in a:
            cause = "re-run densify banner + capture-id"
        elif norm_eq:
            cause = "capture-id handle only"
        else:
            cause = "content differs"
        out.append((label, False, cause))
    return out


# ------------------------------------------------------------------- report
def run_all() -> str:
    with tempfile.TemporaryDirectory(prefix="ctx-plancol-") as td:
        os.environ["CTX_STATE_HOME"] = str(Path(td) / "state")
        root = Path(td) / "proj"
        root.mkdir()
        build_fixture(root)
        n_steps = arm_naive(root)
        b_steps = arm_harness(root)
        p_steps, plan_json = arm_plan(root)

    arms = [
        ("N naive interactive", n_steps),
        ("B harnessed interactive", b_steps),
        ("P compiled plan", p_steps),
    ]

    out: list[str] = ["# plan-collapse: measured transcript economics", ""]
    out.append("| arm | R (crossings) | Σ O_i first-exposure | "
               "C = Σ i·O_i (resend) |")
    out.append("|---|---|---|---|")
    for name, steps in arms:
        fe, rc = first_exposure(steps), resend_cost(steps)
        out.append(
            f"| {name} | {len(steps)} | {nbytes_row(fe)} | {nbytes_row(rc)} |"
        )
    out.append("")
    pj = nbytes(plan_json)
    out.append(f"Arm P model-authored plan JSON (OUTPUT, not resent input): "
               f"{pj:,} B · est {est_tokens(pj):,} tok. Token counts only — "
               "output/input are priced asymmetrically; this harness reports "
               "neither dollars.")
    out.append("")

    # Per-step byte ledger (the raw O_i behind the totals).
    out.append("## Per-step first-exposure bytes (O_i)")
    out.append("")
    for name, steps in arms:
        out.append(f"**{name}**")
        out.append("")
        out.append("| i | step | O_i bytes | est tok |")
        out.append("|---|---|---|---|")
        for i, (label, s) in enumerate(steps, start=1):
            out.append(f"| {i} | `{label}` | {nbytes(s):,} | {est_tokens(nbytes(s)):,} |")
        out.append("")

    # Cache-stability: re-run each arm's steps and diff.
    out.append("## Cache-stability (each arm's steps run twice, unchanged worktree)")
    out.append("")
    out.append("| arm | step | byte-identical | instability source |")
    out.append("|---|---|---|---|")
    for label, build, runner in (
        ("N", build_fixture, arm_naive),
        ("B", build_fixture, arm_harness),
        ("P", build_fixture, arm_plan),
    ):
        for step_label, ident, cause in cache_probe(build, runner):
            mark = "yes" if ident else "no"
            out.append(f"| {label} | `{step_label}` | {mark} | {cause} |")
    out.append("")
    return "\n".join(out)


def nbytes_row(n: int) -> str:
    return f"{n:,} B · {est_tokens(n):,} tok"


if __name__ == "__main__":
    print(run_all())
