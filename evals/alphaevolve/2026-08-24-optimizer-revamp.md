# Optimizer revamp receipt — 2026-08-24

## Scope

The managed-search harness in `evals/alphaevolve/` was upgraded to follow the
documented patterns of Google's
[alphaevolve-on-googlecloud](https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud)
client more closely. This is a search-infrastructure change only: no production
seam, evaluator gate, corpus, promotion rule, or rollout stage changed, and no
new product claim is made.

## What changed

1. **Model mixture (`ensemble.py`).** Managed campaigns can now blend the two
   documented generation models by weight — `gemini-3.5-flash` fast exploration
   plus `gemini-3.1-pro-preview` depth, as Google's examples configure via
   `MODEL_1`/`MODEL_2` + weights. Supplied through `--model-mix` or
   `MODEL_MIX`. Unknown model names fail closed before any experiment is
   created; `gemini-3.1-pro-preview` is rejected unless `LOCATION=global`,
   matching Google's serving rule. Default remains single-model flash, so no
   existing spend profile changes silently.
2. **Memoized campaign evaluator (`campaign.py`).** Identical candidate source
   within one campaign process is evaluated once and replayed locally.
   Normalization strips only semantically inert whitespace, so any token-level
   change misses the memo.
3. **Multi-metric score submission.** Each evaluation now submits the primary
   scalar plus per-metric component log2 gains as separate named scores
   (`<metric>.<component>_log2_gain`), so the managed program database can rank
   on dollars, tokens, turns, tool calls, or latency individually instead of
   only the collapsed scalar. Ranking by the primary metric is unchanged.
4. **Structured failure insights.** Failures return typed hint text (timeout,
   non-determinism, blocked import/name, unsafe/inadmissible choices,
   budget/shape violations) that Google's controller feeds back to the ensemble
   to steer the next generation. Successes report the strongest multiplier and
   weakest metric so search targets the lagging dimension.
5. **Dated campaign artifacts.** Every finished or resumed run writes a
   `ctx.alphaevolve-managed-campaign/v1` summary (config, fingerprint, top
   programs, artifact path) plus the best program source under
   `evals/alphaevolve/results/`, mirroring Google's result/evolved-program
   convention. Artifacts are evidence records; they never write into
   `src/ctx`.
6. **Concurrent fleet scorecard.** `portfolio --all-local` now launches one
   isolated child per experiment up front and collects results concurrently,
   assembling output back in registry order.

## Measured local results

| Measurement | Before | After | Denominator |
|---|---:|---:|---|
| Full-fleet scorecard wall time (`--all-local`, 21 experiments, all gates) | 7.44 s | 2.60 s | same machine, same seeds, byte-identical JSON output |
| Duplicate-candidate evaluation (12 resubmissions of identical source, wave-policy seed) | 1273 ms legacy path | 106 ms memo path (**12x**) | first evaluation still pays full sandbox cost; hits are dict lookups |
| Registry reconciliation | docs said 31 levers / 28 mutable / 20 families | registry truth: **32 levers / 29 mutable / 3 protected / 21 families / 9 waves** | `registry.py`; deployment doc wave table also gained the missing `devex` row |

The scorecard speedup comes from paying child-process boot once per experiment
instead of once per gate (~55 spawns to ~21), not from weakening isolation:
each candidate evaluation still runs twice per call inside a timeout-bounded
spawn child with restricted builtins.

## Verification

- `tests/test_alphaevolve_campaign.py` (new): 15 tests covering mixture
  parsing/validation/global-location rules, memoization including
  whitespace-only dedupe, component-score submission, failure hints, invalid
  envelopes, artifact writing, config embedding, parallel-vs-serial scorecard
  equality, and drop-in compatibility with each pack's legacy adapter.
- Full AlphaEvolve suites: `test_alphaevolve_eval.py`,
  `test_alphaevolve_portfolio.py`, `test_alphaevolve_campaign.py` — 90 passed.
- `python -m evals.alphaevolve.portfolio --all-local`: all gates pass;
  output byte-identical to the pre-change serial scorecard.
- `python -m evals.alphaevolve.run_experiment` preflight: unchanged behavior
  (correctly exits non-zero when `GE_APP_ID` is absent).

## Limitations

- The memo lives per process: resuming a campaign starts a fresh cache. This
  is conservative — it can only re-pay an evaluation, never replay a stale one
  across code changes to evaluators or corpora (fingerprints still change).
- No billed managed campaign was run for this receipt; cloud-side numbers are
  not claimed. The next bounded campaign will record whether duplicate-source
  rates in practice approach the measured ceiling.
- Component metric names are advisory ranking aids; promotion continues to be
  decided from the complete reported vector under the existing contract.
- Python-level containment remains "containment, not an OS sandbox," exactly
  as before.
