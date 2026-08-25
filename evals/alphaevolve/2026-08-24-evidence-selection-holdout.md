# Evidence selection: holdout, translation, exploration-scale — 2026-08-24

## Scope

One small managed AlphaEvolve campaign (14 programs, flash-heavy mixture,
engine `phoenix-retail_1751440313229`) on the original evidence-selection
experiment, then the promotion-contract steps that previous winners never
received: independently authored holdout/adversarial corpora, a reviewed
translation into production, and an exploration-scale corpus built from real
repository command outputs.

## Campaign result

| Corpus | Seed | Best evolved | Delta |
|---|---:|---:|---|
| Search (8 frozen cases) | 75.4198 | **94.4444** | **+25.22%** relative |
| Independent holdout (authored after the campaign; go/npm/psql/k8s families) | 72.857 | **84.732** | +16.3% |
| Adversarial (benign traps, decoys, tail conclusions) | 64.435 | **87.500** | +35.8% |

This beats the historical 2026-08-17 fleet best (91.1161) with fewer
candidates. Because the holdout was authored after the campaign from
different toolchains and still shows +16.3%, the gain is genuine transfer,
not search-corpus memorization. The winner remains a candidate: no live
canary has run, so no billed-savings claim is made.

## What the winner learned

Layered signal ranking replacing one blunt keyword regex: multi-format
source-coordinate extraction (Rust `-->` pointers, Python/Java frames,
`file:line(:col)`), real-vs-zero failure-summary parsing (`1 failed` vs
`0 failures`), HTTP 4xx/5xx status recognition without keywords,
request/correlation-id detection, benign-trip negation ("no errors detected"
must never be selected). Runtime 2.5 ms per selection (seed 0.3 ms).

## Reviewed production translation

The same policy seam exists in production as the generic text fallback's
signal lines (`src/ctx/digest/text.py`), which still used the seed-era blunt
`_ERROR_RE`. Translated by maintainers into `_signal_rank()` tiering with the
same doctrine — benign traps return -1, keyword-less decisive evidence (HTTP
status codes, exit codes/OOMKilled, real failure summaries, quota rejection)
returns 3, coordinate-bearing lines 2, weak keywords 1 — and the digest now
selects its "first signal" by strongest tier instead of first keyword match.
Focused tests: `tests/test_digest_text_signals.py`.

## Exploration-scale corpus (new gate)

Real captured outputs frozen under `fixtures/exploration/` (761-line grep
sweep over `src/ctx`; a 250-shard upload dying in `ModuleNotFoundError`;
a pip externally-managed block; a verbose pytest assertion failure), graded
objectively and scored by `score_exploration()`. This corpus is deliberately
outside managed search.

| Policy | Exploration score |
|---|---:|
| Seed | 42.411 |
| Winner / translated policy | **57.143** (+34.7%) |

Perfect recovery of the buried traceback conclusion under 250 noise lines;
verbose-pytest selection improved 0.571 → 0.786. Two blind spots are now
measured targets for the next campaign: coordinate-saturated ranking (inside
a grep sweep every line carries `file:line`, so tier-2 saturates) and
multi-line remediation blocks (pip's fix spans several lines).

## Verification

- `tests/test_alphaevolve_exploration.py`: corpus integrity, seed validity
  across all four gates, translated-ranking behavior.
- `tests/test_alphaevolve_eval.py`, `test_alphaevolve_portfolio.py`,
  `test_alphaevolve_campaign.py`, `test_alphaevolve_campaign_driver.py`,
  `test_ctx_collab.py`, `test_cliux.py`, `test_cli_dispatch.py`,
  `test_digest_text_signals.py`, head-tail/determinism suites: green.
- Full repository suite: exit 0 (one pre-existing, environment-dependent
  workspace test excluded; it fails identically on a stashed clean tree).

## Limitations

- Scores are frozen-corpus utilities, not billed production savings.
- No matched live canary has run for this policy; until one does, this is
  instrumented-quality evidence only.
- The exploration corpus is four captures from one repository on one day;
  broad toolchain coverage is future work.
- The managed-search winner source stays in `results/` as evidence; the
  production change is the maintainer translation, not generated code copied
  into `src/ctx`.
