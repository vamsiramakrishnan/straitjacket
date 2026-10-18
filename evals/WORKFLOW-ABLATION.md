# Repeated workflow ablations

`workflow_ablation.py` reuses `agentbench/adapters/deepswe.py`: pristine
verification, held-out tests and committed-patch collection retain the existing
referee. This is an internal evidence-preservation experiment. It does not turn
DeepSWE into a public resolve-rate claim.

| Profile | Command view | File navigation | Query/plan tools |
|---|---|---|---|
| rich | Typed JSON | Exact bounded reads | Off |
| compact | Compact text | Exact bounded reads | Off |
| navigation | Compact text | Small direct reads, outlines, symbols | Off |
| composed | Compact text | Small direct reads, outlines, symbols | On |

Every profile uses the full tool replacement surface and the same durable tool
budget. No initial pack is injected. Context reuse is off: the current provider
path cannot establish when model context is evicted. Epoch reuse has separate
model-free acceptance checks. Commands below require a clean source revision,
an explicit provider model and an installed SDK version selected by the operator.

First build the candidate and validate its wheel:

```bash
python -m build
python scripts/check_distribution.py dist/*.whl dist/*.tar.gz
python evals/workflow_replay.py --out /tmp/workflow-replay.json
```

Install that exact wheel and a pinned `google-antigravity` version into a separate
venv. Validate selected DeepSWE fixtures with `agentbench/validate.py` before
paid work. Freeze the experiment (no model call):

```bash
python evals/workflow_ablation.py freeze \
  --wheel dist/ctx_harness-0.40.0-py3-none-any.whl \
  --source-sha "$(git rev-parse HEAD)" \
  --sdk-version "$SDK_VERSION" --model "$MODEL_ID" \
  --tasks /path/to/deep-swe/tasks --ids "$TASK_IDS" \
  --repeats 3 --out /tmp/workflow-lock.json
```

The lock records exact installed file hashes from the wheel (including the
managed provider shim), source commit, provider SDK, model, task/corpus commits,
profile flags, deadline and repeat count. Live preflight rejects a different
runtime, SDK, corpus revision or profile definition. Fixtures must live outside
the implementation checkout. Per-trial artifact state is isolated; profile order
rotates deterministically across tasks and repeats.

```bash
python evals/workflow_ablation.py run --lock /tmp/workflow-lock.json \
  --python /path/to/pinned-venv/bin/python \
  --work-root /tmp/workflow-fixtures --out /tmp/workflow-trials
python evals/workflow_ablation.py report /tmp/workflow-trials/results.json
```

`run` is explicitly live and requires the provider credential. It writes each
completed trial to a partial receipt, preserves error rows, and grades agent
claims independently. It never silently retries a paid trial; use a new output
directory for a new sweep. A process killed mid-trial leaves a partial receipt
whose missing cells prevent it from reading as a complete comparison.

Cost remains unknown unless freeze receives an explicit rate card and known
input/cache semantics. `--rates rates.json --input-semantics excludes_cache`
accepts USD per million tokens under `uncached_input`, `cached_input`, `output`.
Thinking and candidate output counters must both be available; no missing counter
becomes zero. Those are estimates, never invoices. Cost per resolved attempt
includes all failed-attempt spend. Zero successes or any unknown attempt cost
produce a null cost ratio and a separately named known subtotal.

The report separates custom tool calls, delivered bytes, model requests and
agent elapsed time. Provider aggregate usage does not expose model-request
boundaries in this adapter, so request counts stay null. Native edits are not
included in custom tool-call counts. Real request/turn reductions require a
provider observation that supplies those boundaries; byte reduction cannot
establish them. Compare matched outcomes and regressions before interpreting
efficiency. Repeated results include model and environment variance.
