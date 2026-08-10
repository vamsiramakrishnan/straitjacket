#!/usr/bin/env python3
"""The three promises, checked. Model-free, ~30s, CI-gateable.

The test suite checks whether functions are correct. These check whether the
product's claims are still true, which is a different question and the one
nothing was asking:

  1. BOUNDED   the digest stays a fixed size however loud the command was.
               "304,113 -> ~210" is the headline; nothing stopped a profile
               from quietly becoming linear in its input.
  2. CHEAP     the hook runs on every single tool call. Nothing measured what
               that costs, so a regression would surface as "straitjacket
               makes my agent feel slow" rather than as a failing build.
  3. RESOLVABLE every omission carries an address that actually returns bytes.
               This is `evals/BENCHMARK.md`'s unresolved-omission rate, which
               the charter specifies, calls testable without a model, targets
               at 100%, and which was never implemented.

Thresholds are deliberately loose: this is a tripwire for regressions of a
kind nothing else can see, not a benchmark. Tighten them when a number moves
for a reason you understand.

    python evals/invariants.py [--verbose]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 1000x more input may not grow the digest by more than this. Measured at
# ~1.06x when written; the allowance is for census lines on richer output.
MAX_DIGEST_GROWTH = 2.0
# Per tool call, so it multiplies by every action the agent takes.
MAX_HOOK_P95_MS = 150.0
# The core promise. Anything less than everything is a broken address.
MIN_RESOLVE_RATE = 1.0


def _ws(stack: list) -> pathlib.Path:
    td = tempfile.TemporaryDirectory(prefix="invariants_")
    stack.append(td)
    ws = pathlib.Path(td.name)
    subprocess.run(["git", "init", "-q", "."], cwd=ws, capture_output=True)
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return ws


def _run(ws: pathlib.Path, argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=ws, capture_output=True, text=True, timeout=300, **kw)


def check_bounded(ws: pathlib.Path, verbose: bool) -> tuple[bool, str]:
    """Digest size must not track input size."""
    gen = "print('\\n'.join(f'worker {i} step ok checksum=0x{i:08x}' for i in range({n})))"
    sizes = []
    for n in (100, 1_000, 10_000, 100_000):
        r = _run(ws, ["ctx", "run", "--", sys.executable, "-c", gen.replace("{n}", str(n))])
        digest = r.stdout + r.stderr
        sizes.append((n * 40, len(digest)))
        if verbose:
            print(f"      input ~{n * 40:>9,}B -> digest {len(digest):>6,}B")
    first, last = sizes[0][1], sizes[-1][1]
    growth = last / max(first, 1)
    ok = growth <= MAX_DIGEST_GROWTH
    return ok, (f"input grew {sizes[-1][0] // max(sizes[0][0], 1)}x, "
                f"digest grew {growth:.2f}x (limit {MAX_DIGEST_GROWTH}x)")


def check_cheap(ws: pathlib.Path, verbose: bool) -> tuple[bool, str]:
    """Hook latency is paid on every tool call."""
    payload = json.dumps({
        "tool_name": "Bash", "cwd": str(ws),
        "tool_input": {"command": "echo hi"}, "tool_response": "hi",
    })
    ts = []
    for _ in range(15):
        t0 = time.perf_counter()
        _run(ws, ["ctx", "hook", "claude-code", "post-tool-use"], input=payload)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    p50, p95 = statistics.median(ts), ts[int(0.95 * len(ts)) - 1]
    if verbose:
        print(f"      p50 {p50:.0f}ms  p95 {p95:.0f}ms  max {ts[-1]:.0f}ms")
    return p95 <= MAX_HOOK_P95_MS, (f"p50 {p50:.0f}ms, p95 {p95:.0f}ms "
                                    f"(limit {MAX_HOOK_P95_MS:.0f}ms p95)")


# Commands chosen for DIFFERENT profiles: unittest, search, text, and a
# multi-stream case. A per-profile sweep, not one happy path.
_PROBES = [
    ("unittest", ["{py}", "{failing}"]),
    ("search", ["grep", "-rn", "def ", "{root}/src"]),
    ("listing", ["ls", "-laR", "{root}/src"]),
    ("vcs", ["git", "-C", "{root}", "log", "--stat", "-30"]),
]

_FAILING_TEST = '''
import unittest
def f(x): return x * 2
class TestCases(unittest.TestCase):
    def test_a(self): self.assertEqual(f(3), 9)
    def test_b(self): self.assertEqual(f(-4), 16)
    def test_c(self): self.assertEqual(f(5), 25)
import unittest as _u, sys as _s
_s.exit(0 if _u.TextTestRunner(verbosity=0).run(
    _u.TestLoader().loadTestsFromTestCase(TestCases)).wasSuccessful() else 1)
'''


def check_resolvable(ws: pathlib.Path, verbose: bool) -> tuple[bool, str]:
    """Every `ctx ...` a digest offers must actually return bytes.

    An offer that errors costs the agent a turn and teaches it to stop
    trusting the digest -- worse than not offering at all.
    """
    failing = ws / "failing_test.py"
    failing.write_text(_FAILING_TEST, encoding="utf-8")

    total = broken = 0
    for name, template in _PROBES:
        argv = [p.replace("{py}", sys.executable)
                 .replace("{failing}", str(failing))
                 .replace("{root}", str(ROOT)) for p in template]
        proc = _run(ws, ["ctx", "run", "--", *argv])
        # The digest lands on stdout for a clean exit and can carry stderr
        # detail on a failing one; read both rather than guessing.
        digest = (proc.stdout or "") + (proc.stderr or "")
        offers = [ln.strip() for ln in digest.splitlines()
                  if ln.strip().startswith("ctx ")]
        for offer in offers:
            total += 1
            # A <placeholder> is meant to be substituted, not pasted; running
            # it verbatim proves nothing about the address behind it.
            if "<" in offer and ">" in offer:
                continue
            r = _run(ws, shlex.split(offer))
            if r.returncode != 0 or not (r.stdout or "").strip():
                broken += 1
                print(f"      BROKEN [{name}] {offer}\n"
                      f"             rc={r.returncode} {(r.stderr or '').strip()[:120]}")
            elif verbose:
                print(f"      ok  [{name}] {offer[:66]}")
    if total == 0:
        return False, "no offers emitted at all — cannot verify the promise"
    rate = (total - broken) / total
    return rate >= MIN_RESOLVE_RATE, f"{total - broken}/{total} offers resolve"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    stack: list = []
    checks = [
        ("BOUNDED    digest size does not track input size", check_bounded),
        ("CHEAP      hook latency per tool call", check_cheap),
        ("RESOLVABLE every offered address returns bytes", check_resolvable),
    ]
    failed = 0
    try:
        for label, fn in checks:
            print(f"  {label}")
            try:
                ok, detail = fn(_ws(stack), args.verbose)
            except Exception as exc:  # a check that cannot run has not passed
                ok, detail = False, f"check errored: {exc}"
            print(f"      {'PASS' if ok else 'FAIL'}  {detail}\n")
            failed += 0 if ok else 1
    finally:
        for td in stack:
            td.cleanup()

    print(f"invariants: {len(checks) - failed}/{len(checks)} hold")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
