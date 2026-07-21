#!/usr/bin/env python3
"""M-K2 referee: does selecting the file set before scanning pay off?

The substrate claim (docs/SUBSTRATE.md §M-K2, "17 files not 1,482"): an
expensive scan over a corpus-selected subset costs proportionally less
than the same scan whole-repo. This measures it deterministically on this
repository with the shipped structural engine (ast-grep): the file-set
reduction ratio from `corpus`, and the wall-clock of an identical
ast-grep pattern scan scoped to the selected files vs the whole tree.

The semantic-engine arm (Semgrep — the SLOW engine where scoping matters
most) is declared, not run: Semgrep is not installed in this environment.
The structural arm still shows the direction; the file-count ratios are
the hard, engine-independent receipt.

Usage: python3 evals/corpus_scoped_scan.py [--pattern P] [--repeats 5]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _corpus(ws, **kw) -> tuple[list[str], dict]:
    from ctx import filesets

    rows, coverage, _ = filesets.select(ws, **kw)
    return [r["file"] for r in rows], coverage


def _astgrep_scan(root: Path, pattern: str, paths: list[str]) -> tuple[int, float]:
    """Run ast-grep for a pattern over the given paths; return (matches,
    seconds). Empty paths → no scan (0, 0)."""
    if not paths:
        return 0, 0.0
    argv = ["ast-grep", "run", "--pattern", pattern, "--lang", "python",
            "--json=stream", *paths]
    t0 = time.monotonic()
    proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=120)
    dt = time.monotonic() - t0
    matches = sum(1 for ln in proc.stdout.splitlines() if ln.strip().startswith("{"))
    return matches, dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default="def $NAME($$$)")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from ctx.workspace import resolve_workspace

    root = Path(__file__).resolve().parent.parent
    ws = resolve_workspace(str(root))

    whole, cov_whole = _corpus(ws, exts=["py"])
    scoped, cov_scoped = _corpus(ws, exts=["py"], globs=["src/ctx/_retrieval/**"])

    def timed(paths: list[str]) -> tuple[int, float]:
        runs = [_astgrep_scan(root, args.pattern, paths) for _ in range(args.repeats)]
        return runs[0][0], _median([dt for _, dt in runs])

    m_whole, t_whole = timed(whole)
    m_scoped, t_scoped = timed(scoped)

    reduction = (1 - len(scoped) / len(whole)) * 100 if whole else 0.0
    speedup = (t_whole / t_scoped) if t_scoped else float("inf")
    result = {
        "pattern": args.pattern,
        "repeats": args.repeats,
        "whole": {"files": len(whole), "considered": cov_whole["considered"],
                  "matches": m_whole, "median_s": round(t_whole, 3)},
        "scoped": {"files": len(scoped), "glob": "src/ctx/_retrieval/**",
                   "matches": m_scoped, "median_s": round(t_scoped, 3)},
        "file_reduction_pct": round(reduction, 1),
        "wall_speedup_x": round(speedup, 1),
        "semgrep_arm": "declared, not run (semgrep not installed)",
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"pattern: {args.pattern!r}  (ast-grep, median of {args.repeats})")
        print(f"  whole:  {result['whole']['files']:4d} files · "
              f"{result['whole']['matches']} matches · {result['whole']['median_s']}s")
        print(f"  scoped: {result['scoped']['files']:4d} files "
              f"(src/ctx/_retrieval/**) · {result['scoped']['matches']} matches · "
              f"{result['scoped']['median_s']}s")
        print(f"  file-set reduction: {result['file_reduction_pct']}% · "
              f"wall speedup: {result['wall_speedup_x']}×")
        print(f"  semgrep arm: {result['semgrep_arm']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
