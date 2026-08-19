# AlphaEvolve optimization charter

## North-star outcome

Straitjacket should reduce the total cost of completing a real coding task:

1. complete the requested task correctly;
2. preserve every fact needed to diagnose, implement, and verify it;
3. minimize model-visible context that did not contribute to completion;
4. minimize avoidable tool/model turns and repeated retrieval;
5. minimize success-adjusted dollar cost and wall time.

The order is binding. A cheaper, shorter, or smaller-context run that fails the
task is not an improvement.

## What AlphaEvolve has changed in the product

AlphaEvolve is not presented here as an autonomous source-code author. It is
the policy-search and counterexample engine in a controlled improvement loop:

1. Straitjacket records a naive/control comparison and exposes any regression.
2. A small AlphaEvolve experiment searches one decision seam against frozen
   completion, safety, cost, context, turn, and latency gates.
3. Generated candidates remain quarantined. A maintainer translates the
   winning policy into ordinary production code and focused tests.
4. The full repository suite and an independently measured product path decide
   whether the reviewed implementation may enter an instrumented canary.
5. Actual-usage receipts feed the next iteration; missing usage is never
   counted as zero cost.

The first end-to-end product example is the named-test regression:

| Stage | Evidence or change |
|---|---|
| Regression found | The actual-usage iteration showed direct naive execution was **8.55% cheaper** for one warm, explicitly named pytest target. The proposed compact route was rejected rather than promoted. |
| Policy learned | The emission experiment selected `raw_small` for small non-derived output; the engagement experiment selected the passive/bypass state until truncation, window pressure, or repeated use proves the task is no longer small. |
| Reviewed translation | Claude Code and Codex may run exactly one `path::node` pytest target natively while passive and before that signature has flooded. Broad suites, shell expressions, active sessions, strict steering, and Antigravity retain birth-time capture. |
| Safety closure | PostToolUse remains fail-closed. An unexpected flood becomes a typed, addressable digest and records an intervention, so the next identical signature returns to capture-at-birth. Protected secret, workspace, receipt, and usage oracles were not made mutable. |
| Measured result | Across 11 alternating local repetitions, median latency fell **642.306 ms → 512.910 ms (20.15%)** and visible tool-result bytes fell **150 B → 80 B (46.67%)**, with 11/11 successful executions in each arm. A 41,939-byte synthetic pytest failure became an 871-byte digest: **48.15× containment / 97.92% fewer bytes**. |

The user-facing benefit is conditional efficiency: a task that is demonstrably
small gets naive-like direct execution, while an unexpected large result still
gets lossless, addressable containment. Before this optimization, users paid
the fixed wrapper cost on both paths. After it, the fixed cost is avoided on
the proven-small path and restored automatically for a signature once it has
flooded. This is why the improvement is algorithmic rather than a narrower
formatting or implementation-speed tweak.

This is a shipped, reversible, instrumented canary controlled by
`[guard].speculative_native`. It fixes the known local wrapper-tax regression;
it is **not yet evidence of lower billed end-to-end production cost**. That
claim still requires matched live canaries populated from
`ctx.steering-decision/v1`, `ctx.steering-result/v1`, and provider
`actual_usage` receipts. See the
[dated integration receipt](../evals/alphaevolve/2026-08-18-speculative-native.md).

## Measurement hierarchy

AlphaEvolve maximizes numeric scores, while straitjacket's release doctrine is a
Pareto surface. Each experiment therefore uses a lexicographic score:

- **Validation gate:** deterministic API shape, bounds, no unsafe effects.
- **Completion gate:** all mandatory evidence or required task outcomes survive.
- **Quality:** retain decisive evidence and choose actions/routes that can finish.
- **Efficiency:** only after the gates pass, reward fewer visible tokens, fewer
  turns, lower measured dollars where actual usage is complete (otherwise
  explicitly labeled estimates), and lower evaluator runtime.

An invalid candidate receives `-1_000_000`. A candidate that is valid but loses
a task-critical fact receives a large completion penalty. Secondary metrics are
recorded separately even when AlphaEvolve searches on one scalar objective.

The optimization unit is **success-adjusted cost**:

```text
total cost to completion
  = model dollars
  + model-visible context tokens
  + retrieval/tool turns
  + unresolved-evidence regret
```

No fixed exchange rate between dollars, tokens, and turns is claimed as product
truth. Frozen evaluator weights guide search; promotion is decided from the
reported component metrics and a live matched-model A/B.

## Experiment portfolio

### 1. Evidence selection — completed

Choose the best generic-output lines under a hard line budget. The first run
improved frozen `evidence_utility` from 75.419820 to 91.116070. Its candidate is
not promoted until it passes an independent holdout corpus.

### 2. Context-budget allocation

Allocate a token budget across evidence items. Mandatory root cause, identity,
verification, and retrieval-address facts must survive. Among complete
selections, prefer higher evidence utility and fewer visible tokens. This aims
directly at context bloat without rewarding silent omission.

### 3. Turn-minimizing next action

Given a task state and bounded available operations, choose one next action at a
time. A deterministic simulator reveals facts and advances the state. Completion
requires all task facts; repeated, irrelevant, or unsafe actions fail or cost
turns. This searches for policies that use `ctx q`, maps, focused retrieval, and
verification instead of serial exploratory loops.

### 4. Success-adjusted execution routing

Choose a host/model route from task risk, complexity, capabilities, token
estimates, and price. Every frozen task must complete with a capable route.
Among passing policies, prefer lower dollars, lower input context, and fewer
expected repair turns. This is a candidate policy for the deterministic
orchestrator fallback, not permission to launch agents automatically.

### 5. Naive-use-case fast path

Start from a deliberately naive policy that sends every simple request through
a broad-context standard route. Evolve narrow one-shot paths for supplied
context, named symbols, known files, named tests, diffs, and small verified
edits. Every task must still complete. A candidate must Pareto-beat the broad
baseline on visible tokens, model turns, tool calls, and estimated dollars, and
must also dominate a cheap no-op baseline on completion. Independently authored
holdouts cover supplied logs, known-document edits, change review, and locating
an initially unknown small bug.

### 6. Receipt-informed route replay

Join privacy-safe `ctx.route-run/v1` execution receipts to separate, explicit
semantic labels. Live observations constrain host/model admissibility; contract
cases fill task shapes that are unsafe or wasteful to exercise just to generate
data. A mutation may optimize actual dollars where complete usage exists, plus
estimated dollars, visible tokens, model turns, tool calls, and wall time only
after unattended execution, required capabilities, mutation verification, and
matching live-evidence gates pass. `partial` and `unavailable` usage remain
first-class states; the evaluator must never interpret missing usage as free.

The replay snapshot is refreshable with:

```bash
python -m evals.alphaevolve.route_replay.snapshot . \
  --output evals/alphaevolve/route_replay/observations.json
```

Pass additional disposable workspace roots before `--output` to merge their
labeled receipts; observations are deduplicated by run ID. Refreshing an
existing output preserves reviewed runs whose disposable workspace no longer
exists. Use `--replace` only when deliberately rebuilding the corpus from the
supplied roots.

Snapshot review is mandatory: labels are human/acceptance evidence, not inferred
from exit code, and a sparse corpus must not be presented as broad production
proof.

The second live iteration also treats coordinator pins, downstream mutation
verification, and explicit host failure reports as completion gates. A zero-exit
host response such as permission auto-denial, read-only blockage, or an explicit
"task not complete" verification report is an execution failure, not a cheap
success.

### 7. Recovery and escalation policy

Choose among focused retrieval, same-model retry, replan, stronger-model
escalation, and an honest blocked/budget stop from typed failure evidence.
Completion or correct terminal disposition is a hard gate. Authentication,
permission, and safety failures must not spend another model turn; missing
evidence should retrieve before escalating; incomplete contracts and failed
verification should replan. Among correct recoveries, minimize added actual or
estimated dollars, model attempts, and latency.

## Search and spend bounds

- Python only, one `EVOLVE-BLOCK` per experiment.
- Initial portfolio runs: at most 12 programs per new experiment, concurrency 2.
- One Gemini Enterprise engine/assistant; no IAM mutation by a runner.
- Every cloud command requires `--confirm-spend`.
- Generated code runs in a timeout-bounded child process with restricted
  builtins and allowlisted pure-stdlib imports.

## Promotion contract

An AlphaEvolve winner is a hypothesis. Promotion requires all of:

1. score improvement on the hidden search corpus;
2. no regression on an independently authored holdout corpus;
3. adversarial cases for gaming, false positives, missing evidence, unsafe
   actions, and pathological size/runtime;
4. deterministic byte-identical behavior where the production contract
   requires it;
5. the complete unit/acceptance/evaluation suite;
6. a matched-model live A/B measuring task success, model-visible tokens,
   tool/model turns, dollars, wall time, and unresolved omissions;
7. a dated receipt that reports losses and reversals, not only wins.

Production integration is always a reviewed source change. A managed experiment
never writes directly into `src/ctx`.

“Promotion” has two explicitly different meanings:

- **Instrumented product canary:** reviewed code may ship behind a narrow,
  reversible condition after deterministic completion/safety gates and a
  matched local path measurement. No billed-cost claim is allowed.
- **Proven performance promotion:** requires the matched-model live A/B in step
  6 above, including task success and complete actual usage.

The named-test change is currently the first category, not the second.

The fleet-wide registry, shared multiplicative evaluator, deployment waves,
shadow reports, and promotion commands are specified in
[AlphaEvolve deployment](ALPHAEVOLVE-DEPLOYMENT.md).

## The 100x campaign

`100x` is a portfolio target, not a promise that every task becomes one hundred
times cheaper. The campaign measures each waste dimension against a naive path
and requires equal task completion:

| Surface | Naive denominator | Stretch target | Completion guard |
|---|---|---:|---|
| Raw tool-output containment | model-visible raw bytes/tokens | 100x | decisive evidence remains retrievable |
| Repeated context | uncached repeated input | 100x | every required fact survives |
| Turn policy | avoidable model/tool attempts | 10x, then 100x where possible | same verified outcome |
| Routing | dollars to verified completion | 2x–10x typical; 100x only on pathological routes | same task and capability |
| Recovery | wasted attempts after typed failure | eliminate impossible retries | correct recovery or honest stop |

The right algorithm is conditional: contain aggressively when output is huge,
retrieve narrowly when evidence is addressable, use the direct fast path when
the task is already small, and escalate only from typed evidence. A result is
reported as a vector (completion, dollars, visible tokens, turns, tool calls,
latency), never collapsed into an unsupported blanket percentage.

The dated [actual-usage iteration](../evals/alphaevolve/2026-08-18-actual-usage-iteration.md)
is the first explicit
naive-vs-Straitjacket promotion receipt. Direct naive won that small named-test
cost probe by 8.55%; the proposed compact prompt was rejected. Negative results
remain in the corpus so future search cannot rediscover and silently promote
the same regression.

### Current measured frontier

| Measurement | Improvement | Evidence class |
|---|---:|---|
| Quiet-needle raw context | **578x smaller** with the needle retained and addressable | deterministic field corpus |
| Unavoidable Antigravity flood | **152x less tool output**, 30% fewer billed tokens, equal correctness | live matched-host A/B |
| Integrated naive fast path, search mix | **11.25x less visible context**, 9.09x lower modeled dollars, 3x fewer model turns | frozen completion-gated evaluator |
| Receipt-informed routing, search mix | **2.81x lower blended dollars**, 2.25x less visible context | replay; only 1 of 8 case costs currently actual |
| Small warm named-test route | **0.92x** cost versus direct (an 8.55% loss) | one live matched-model probe |
| Guarded native named-test path | **20.15% lower local median latency**, 46.67% fewer tool-result bytes versus always-captured; unexpected failure contained 48.15x | 11-repeat local path benchmark + real emission gate |

The containment mechanism has therefore already exceeded 100x on the dimension
where it is designed to operate. End-to-end dollars have not improved 100x, and
the table does not imply that they have. See the
[field needle receipt](../evals/field-needle-2026-07-20.md),
[Antigravity A/B](../evals/antigravity-gemini-2026-07-19.md), and the local
scorecard for the denominators and completion gates.
The guarded-native numbers are documented in the
[promotion receipt](../evals/alphaevolve/2026-08-18-speculative-native.md);
they show that the identified fixed-tax regression is removed locally, not yet
that billed end-to-end cost improved in production.
