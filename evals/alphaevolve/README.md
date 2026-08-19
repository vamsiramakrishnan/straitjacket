# AlphaEvolve optimization portfolio

The portfolio's north-star objective, metric hierarchy, spend bounds, and
promotion contract are in
[`../../docs/ALPHAEVOLVE-OPTIMIZATION.md`](../../docs/ALPHAEVOLVE-OPTIMIZATION.md).

The portfolio searches bounded policy seams without allowing generated code
into `src/ctx`. Each experiment exposes a small mutable `EVOLVE-BLOCK`; its
evaluator is deterministic, model-free, budget-aware, and uses a frozen corpus
outside the problem prompt. A result is a candidate, not a release: production
integration is a separately reviewed source change with focused tests, the full
suite, and a dated receipt.

## What has reached the product

The first reviewed integration is the guarded native path for one explicitly
named pytest node. The evidence chain is intentionally visible:

1. `actual_usage` comparison found direct naive was 8.55% cheaper on the small
   named-test case;
2. the emission and engagement policies selected raw-small/passive behavior
   while preserving the flood gate;
3. maintainers translated that policy into `ctx.hook` and `ctx.reflex` rather
   than copying generated code;
4. the matched local product path improved median latency 20.15% and
   tool-result bytes 46.67%; an unexpected synthetic failure was still
   contained 48.15× and remained addressable.

This is an instrumented canary, not a billed-production savings claim. See
[`2026-08-18-speculative-native.md`](2026-08-18-speculative-native.md) for the
fixture, denominators, safety boundary, verification, and limitations.

The first bounded run is recorded in
[`2026-08-17-result.md`](2026-08-17-result.md). It completed 19 evolved
candidates and improved the frozen objective from 75.419820 to 91.116070; the
winner remains unpromoted pending independent holdout evaluation.

The completion/context/turn/route portfolio and naive-fast-path campaigns are
recorded in
[`2026-08-17-portfolio-result.md`](2026-08-17-portfolio-result.md). The naive
winner beat the broad baseline on search and holdout cases but remains
quarantined after an adversarial reversal.

The first receipt-informed route replay is recorded in the same portfolio
receipt. Its best evolved candidate tied the reviewed production-shaped seed,
so no generated policy was promoted. The experiment pack includes a prompt-free
observation snapshot and a refresh/check command for iterative live evidence.

The next managed iteration, including actual-usage route calibration and the
new recovery/escalation surface, is recorded in
[`2026-08-18-managed-portfolio-result.md`](2026-08-18-managed-portfolio-result.md).
Both bounded campaigns found plateaus rather than promotable winners; rejected
and tied candidates are reported explicitly.

The fleet-wide registry, nine newly covered policy families, protected oracle
planes, and local gate result are recorded in
[`2026-08-18-fleet-deployment.md`](2026-08-18-fleet-deployment.md).

The first reviewed product canary from those findings—the guarded native path
for one named pytest node—is recorded in
[`2026-08-18-speculative-native.md`](2026-08-18-speculative-native.md). It
includes the matched local wrapper comparison, real fallback-gate measurement,
scope limits, and verification evidence.

Follow Google's [environment and API access setup](https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/developer-guide/environment-and-api-access-setup)
first. AlphaEvolve needs a Gemini Enterprise engine and license in a billed
project, the Discovery Engine and Vertex AI APIs, and the documented system-user
and service-account roles. Install the client from Google's
[official repository](https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud);
it is intentionally not a runtime dependency of `ctx-harness`.

Set only resource identifiers in the environment (never commit credentials):

```bash
export PROJECT_ID=your-billed-project
export GE_APP_ID=your-gemini-enterprise-engine
export LOCATION=global
export COLLECTION=default_collection
export ASSISTANT=default_assistant
```

Run the read-only/local preflight first:

```bash
python -m evals.alphaevolve.run_experiment
```

Run every registered portfolio seed locally before spending:

```bash
python -m evals.alphaevolve.portfolio --list
python -m evals.alphaevolve.portfolio context-budget --local
python -m evals.alphaevolve.portfolio turn-policy --local
python -m evals.alphaevolve.portfolio route-policy --local
python -m evals.alphaevolve.portfolio naive-fast-path --local
python -m evals.alphaevolve.portfolio route-replay --local
python -m evals.alphaevolve.portfolio escalation-policy --local
```

The complete registry now spans 24 production levers and 15 experiment
families. Use the registry and wave commands instead of maintaining a manual
command list:

```bash
python -m evals.alphaevolve.portfolio --list-levers
python -m evals.alphaevolve.portfolio --wave containment --all-local
python -m evals.alphaevolve.portfolio --ready-for-managed
python -m evals.alphaevolve.portfolio --promotion-report
```

See the [deployment plane](../../docs/ALPHAEVOLVE-DEPLOYMENT.md) for the shared
multiplicative metric, protected planes, shadow/canary stages, and complete
lever mapping.

Run every search seed plus every available holdout/adversarial promotion gate
as one algorithm-wide scorecard. Where an integrated policy differs from the
deliberately naive search seed, the scorecard evaluates the integrated policy
and reports the baseline-over-candidate multipliers directly:

```bash
python -m evals.alphaevolve.portfolio --all-local
```

Each new experiment defaults to the charter's 12-program, concurrency-2 cap:

```bash
python -m evals.alphaevolve.portfolio context-budget \
  --run --confirm-spend
```

It checks the seed locally and reads enabled-service state. License assignment,
IAM bindings, engine identity, and Application Default Credentials remain manual
checks because they involve organization-specific authority.

Inspect a managed candidate locally, including any holdout and adversarial
scorers exposed by its experiment pack:

```bash
python -m evals.alphaevolve.portfolio naive-fast-path \
  --inspect-experiment '<full-resource-name>' \
  --inspect-program '<program-id>'
```

Start a deliberately small billed experiment only after reviewing the printed
preflight and the cloud console:

```bash
python -m evals.alphaevolve.run_experiment \
  --run --confirm-spend --max-programs 20 --concurrency 2
```

The command prints the full experiment resource name as soon as it is created.
If the local controller stops while the managed experiment is still running,
reconnect without creating a duplicate:

```bash
python -m evals.alphaevolve.run_experiment \
  --resume-experiment '<full-resource-name>' \
  --confirm-spend --max-programs 20 --concurrency 2
```

Run the controller in a disposable environment. The evaluator blocks imports
and dangerous syntax and enforces a child-process timeout, but Python-level
containment is not a hardened OS sandbox.
