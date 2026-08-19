# AlphaEvolve deployment plane

Straitjacket deploys AlphaEvolve as a fleet of small, completion-gated policy
experiments. It does not expose the repository as one mutable program. That
keeps credit assignment local and prevents a candidate from changing the
safety or measurement rules used to judge it.

## Coverage

The versioned registry in `evals/alphaevolve/registry.py` currently contains:

- 24 production levers;
- 21 mutable levers mapped to 15 experiment families;
- three immutable oracle planes: actual-usage accounting, secret/workspace
  guards, and receipt integrity.

The experiment families cover evidence selection, context budgets, next-turn
policy, routing, naive fast paths, receipt replay, recovery, capability
surfaces, profile detection, output delivery, retrieval, birth-gate policy,
engagement/reflex behavior, plan compilation, repository context, background
execution, and cache materialization.

Each registry entry names its production seam, candidate API, evidence class,
risk, deployment wave, and strongest permitted rollout stage. Dataset files are
hashed into a 16-character fingerprint so results cannot silently move when a
corpus changes.

## Shared metric contract

Discrete experiments use `evals/alphaevolve/choice_eval.py`. A candidate runs
twice per case in the restricted child-process sandbox. A result is rejected if
it is non-deterministic, unsafe, inadmissible, or lacks any required completion
capability.

Passing candidates are compared with the case's naive or complete baseline on:

- dollars;
- model-visible tokens;
- model turns;
- tool calls;
- latency.

The search score uses median and lower-decile `log2(baseline / candidate)`
gains. This makes multiplicative improvements additive while preventing one
large flood win from hiding a small-case reversal. The scalar guides search;
promotion still reviews the complete metric vector.

## First reviewed product integration

The first fleet result translated into production is the speculative-native
named-test canary. It addresses a measured reversal: direct naive execution was
8.55% cheaper than Straitjacket's always-captured path for one warm named
pytest target.

The emission and engagement experiments supplied the conditional structure,
not a patch copied into production:

```text
one explicit pytest path::node
  + passive session
  + no prior flood for this signature
  + host can replace PostToolUse output
    -> run natively
otherwise
    -> keep birth-time capture

if native output crosses the gate
    -> persist + emit typed digest + record intervention
    -> capture the next same-signature call at birth
```

Maintainer-reviewed code implements the policy in `ctx.hook`, with adaptive
state and privacy-safe receipts in `ctx.reflex` and a reversible
`[guard].speculative_native` setting. AlphaEvolve candidates did not receive
write access to production, the safety classifier, usage accounting, or receipt
labels.

The matched local product-path result is 20.15% lower median latency and 46.67%
fewer tool-result bytes over 11 repetitions per arm. The real fallback gate
contained a 41,939-byte synthetic pytest failure to 871 bytes (48.15×) while
retaining a working `run:` address. These are local canary measurements—not a
claim of billed production savings. Full method and limitations are in the
[dated receipt](../evals/alphaevolve/2026-08-18-speculative-native.md).

## Commands

Inventory every lever, including protected planes:

```bash
python -m evals.alphaevolve.portfolio --list-levers
```

Run every experiment, or one wave, through search, holdout, and adversarial
gates:

```bash
python -m evals.alphaevolve.portfolio --all-local
python -m evals.alphaevolve.portfolio --wave containment --all-local
python -m evals.alphaevolve.portfolio --wave retrieval --all-local
```

Ask which experiments may enter managed search. `ready` means only that local
gates pass; it is not a production promotion:

```bash
python -m evals.alphaevolve.portfolio --ready-for-managed
python -m evals.alphaevolve.portfolio --wave execution --ready-for-managed
```

Run a counterfactual local shadow report. It never mutates production:

```bash
python -m evals.alphaevolve.portfolio surface-policy --shadow
```

Review why no current seed or candidate is authorized for production:

```bash
python -m evals.alphaevolve.portfolio --promotion-report
```

Start a bounded managed campaign only with explicit spend confirmation:

```bash
python -m evals.alphaevolve.portfolio retrieval-policy \
  --run --confirm-spend --max-programs 12 --concurrency 2
```

## Waves

| Wave | Experiments | Default rollout |
|---|---|---|
| containment | surface, profile, emission, evidence budget | offline or shadow |
| retrieval | retrieval strategy and budget | shadow |
| birth-gate | command rewrites and read pressure | shadow by default; one named-test arm in an instrumented canary |
| behavior | engagement and reflex circuit | shadow |
| planning | plan compiler, operator ordering, turn policy | shadow |
| context | repository fileset/context selection | shadow |
| execution | backgrounding and cache freshness | shadow |
| orchestration | fast paths, routes, DAGs, recovery | matched live canary |

## Promotion lifecycle

1. Frozen local search corpus.
2. Independently authored holdout and adversarial gates.
3. Bounded managed AlphaEvolve search.
4. Local inspection of the managed winner against every gate.
5. Production shadow decision with no behavioral effect.
6. Opt-in low-risk canary with paired naive/control receipts.
7. Reviewed translation into production source.
8. Automatic rollback on completion, evidence, safety, actual-cost, or
   worst-decile latency regression.

Generated code never writes directly to `src/ctx`. Mutable candidates cannot
import production modules, perform I/O, inspect credentials, change labels, or
alter the usage/accounting oracle.

## Current boundary

The fleet is complete through managed-search readiness and offline
counterfactual shadow reports. One low-risk, reversible named-test policy has
advanced to an instrumented product canary after reviewed translation, full
tests, and a matched local path measurement. Every broader birth-gate,
retrieval, context, execution, and routing mutation remains shadow or canary
only according to the table above.

No AlphaEvolve result currently supports a general billed-production savings
claim. The next gate is a matched live canary joining semantic completion to
provider `actual_usage`; completion, evidence, safety, cost, or worst-decile
latency regression rolls the policy back. Deploying the optimizer means
deploying this measurement and rollback discipline, not automatically shipping
every generated mutation.
