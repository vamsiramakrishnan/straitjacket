# Fleet deployment receipt — 2026-08-18

## Scope

The AlphaEvolve deployment plane expanded from six portfolio experiments to a
versioned fleet covering every identified Straitjacket decision surface.

- 24 registered production levers;
- 21 mutable levers;
- 15 experiment families;
- three protected oracle planes;
- one restricted deterministic child-process runner;
- one shared multiplicative metric contract;
- search, independent holdout, and adversarial gates for every experiment.

Protected planes are actual-usage accounting, secret/workspace guards, and
receipt integrity. They judge candidates and are not candidate mutation
targets.

## Newly covered families

- capability-surface compilation;
- digest-profile selection;
- small-result pass-through and output delivery;
- retrieval strategy and budgets;
- command/read birth-gate decisions;
- graduated engagement and reflex circuits;
- evidence-plan compilation and operator ordering;
- repository fileset/context selection;
- background execution and cache freshness.

The previous context, turn, route, naive-fast-path, route-replay, and recovery
experiments remain registered under the same controller.

## Local result

All 15 seed or integrated policies passed their search, holdout, and adversarial
gates. This establishes managed-search readiness, not production promotion.
Every promotion report remains `production_promotion: false` until a managed
winner is independently inspected and a matched live canary succeeds.

The new adversarial retrieval cases found and corrected one seed ordering bug:
when both a symbol and a failure were present, references ran before typed
failure evidence. Failure/root-cause retrieval now has precedence.

The shared metric has a direct referee proving that a uniform 100x reduction is
reported as a 100x baseline multiplier and the expected robust `log2` gain.
Separate tests prove that the optimizer cannot choose a cheap unsafe guard
decision or reuse a same-size, stale cache entry.

## Commands exercised

```bash
python -m evals.alphaevolve.portfolio --list-levers
python -m evals.alphaevolve.portfolio --all-local
python -m evals.alphaevolve.portfolio --wave containment --all-local
python -m evals.alphaevolve.portfolio --wave retrieval --ready-for-managed
python -m evals.alphaevolve.portfolio surface-policy --shadow
python -m evals.alphaevolve.portfolio --wave execution --promotion-report
```

No new billed managed campaign and no production canary was started by this
implementation. Those remain explicit, separately reviewable deployment steps.
