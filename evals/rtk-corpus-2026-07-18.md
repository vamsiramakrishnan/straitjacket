# rtk-inspired wave: real-corpus measurements before building (2026-07-18)

Method: generated REAL outputs from real tools in seeded projects (eslint
9.x flat-config, tsc strict, ruff 2026 rustc-style output, cargo, go, npm,
pip, git, ls), ran each under the current `ctx run`, measured raw vs digest
tokens and which profile claimed it. Hypotheses were revised by the data
before any code was written.

## What the measurements said (before)

| corpus | raw tok | digest tok | ratio | profile |
|---|---|---|---|---|
| eslint (700 violations) | 13,159 | 141 | 92.8× | text/v1 |
| ruff (new format) | 9,371 | 101 | 92.8× | text/v1 |
| cargo build errors | 2,555 | 134 | 19.0× | text/v1 |
| tsc errors | 1,587 | 138 | 11.5× | text/v1 |
| git status | 307 | 373 | **0.8×** | text/v1 |
| ls -laR | 530 | 596 | **0.9×** | text/v1 |
| pip install | 337 | 740 | **0.5×** | text/v1 |

Two hypothesis reversals:

1. **Compression was NOT the diagnostics gap** — text/v1 already hit 92×.
   The gap was *utility*: a 141-token digest of 700 violations named none
   of them (no counts, no rules, no files). The rtk lesson translated
   correctly is *structure at equal budget*, not more squeezing.
2. **The real embarrassment was inflation of small outputs** — headers,
   indentation, and coverage scaffolding made digests of small successful
   runs *larger than the output itself* (pip 0.5×). Our tax was overhead.

Killed by measurement: npm/pip-install profiles (modern npm prints 349
bytes; inline handles it), git-status/ls profiles (small; the scaffold fix
covers them). Also caught live: ruff's 2026 rustc-style format broke the
first regex draft — synthetic corpora would never have caught it.

## What shipped (v0.11.0) and the after-measurements

- **lint/v1 profile** — eslint-stylish, ruff (old + rustc-style), tsc,
  cargo/rustc, go, mypy shapes; exact totals by severity, by rule, by
  file (shortened paths), first-diagnostic region inlined + span.
- **Scaffold-slim inline emission** — small complete outputs now emit
  command + exit + unindented content: overhead dropped from ~100-400
  tokens to ~15-25 per run.
- **Failure-asymmetric budgets** — `failure_budget_factor` (default 2.0):
  a failing run's digest gets twice the emission budget of a success.
  Success output is boilerplate; failure output is evidence.
- **`ctx gain`** — cumulative containment savings from telemetry, by verb,
  with token and dollar framing. (rtk's sharpest product lesson: the
  metric users can watch is the metric that keeps the harness on.)

| corpus | before | after | profile | utility change |
|---|---|---|---|---|
| eslint | 141 tok, says nothing | 305 tok | lint/v1 | exact: 700 errors, 6 rules ranked, 4 files, region+span |
| ruff | 101 tok, says nothing | 177 tok | lint/v1 | exact: 96 diags, F841×80 F401×16, per-file |
| cargo | 134 tok | 195 tok | lint/v1 | exact counts + codes |
| tsc | 138 tok | 267 tok | lint/v1 | exact TS-code census |
| small runs | 0.5–0.9× (inflated) | ≈1.0× + ~20 tok | text/v1 slim | scaffold tax removed |

The diagnostics digests deliberately spend 60–160 *more* tokens than the
blind text digest — buying exact, decision-grade structure. Whether that
purchase pays is measured in turns, not tokens: see the live lint-fix
benchmark appended below.
