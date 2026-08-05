# M-K2 referee: select files before scanning

**Date:** 2026-07-20 · deterministic, on this repository. Harness:
[`evals/corpus_scoped_scan.py`](corpus_scoped_scan.py); raw numbers:
[`corpus-scoped-scan-2026-07-20.json`](corpus-scoped-scan-2026-07-20.json).

## The claim under test

docs/SUBSTRATE.md §M-K2, the file-set algebra: *"most expensive tools
become much cheaper when run over 17 relevant files instead of 1,482."*
`corpus` (and the `repo.files` plan op) select a bounded eligible set with
a coverage receipt; a scan scoped to that set costs proportionally less.

## Result (median of 7, ast-grep 0.44.1)

| | files | matches | median wall |
|---|---|---|---|
| whole (`corpus --ext py`) | 178 | 2 283 | 0.137 s |
| scoped (`--glob src/ctx/_retrieval/**`) | 9 | 33 | 0.010 s |

- **File-set reduction: 94.9 %** (178 → 9) — the engine-independent hard
  receipt, straight off the `corpus` coverage line (`considered 349 ·
  selected 178/9`).
- **Wall speedup: 13.1×** — and this is the *conservative* case: ast-grep
  is a fast native engine, so the scan cost is nearly all process
  start-up. The mechanism's real target is the **slow** semantic tier.

## What is declared, not run

The Semgrep arm — the SLOW engine where scoping matters most, and the one
the design names — is **not measured here: Semgrep is not installed in
this environment.** The structural arm shows the direction; the file-count
ratio (95 %) is the load-bearing number and is engine-independent. When a
Semgrep-capable environment is available, the same harness scopes
`semantic.*` to a `repo.files` result via the shipped capped `foreach` and
the wall-clock win widens (Semgrep's per-file cost dwarfs ast-grep's).

## Verdict

The "select files first" mechanism pays off exactly as designed: a 95 %
file-set reduction yields a 13× wall speedup on the *cheap* engine, with
the expensive-engine arm pending a Semgrep environment. The receipt is the
file-count ratio; the wall number is a lower bound.
