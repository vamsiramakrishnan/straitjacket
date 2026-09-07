# Bounded semantic analysis

`ctx semantic` applies an explicitly configured model worker to selected evidence.
The host chooses a question and file/artifact handles; straitjacket freezes the
bytes, partitions them, validates the returned citations, and saves progress.
The host receives a bounded report with handles to the full findings and evidence.

This is the external-context map primitive from the
[Recursive Language Models design](https://alexzhang13.github.io/blog/2025/rlm/).
Version 1 has depth one and one worker at a time. It supplies no persistent
Python heap, autonomous recursion, or new edit authority. Ordinary `get`,
`search`, `ask`, digests, and the observation MCP tool never invoke it implicitly.

## Prepare, execute, resume

Save a request inside the workspace. The command below must name a trusted
driver implementing the protocol in the next section; it is an argv array,
executed without a shell. Use absolute paths for driver scripts because each
attempt starts in a fresh temporary working directory.

```json
{
  "schema": "ctx.semantic/v1",
  "question": "Where can cancellation leave descendant processes alive?",
  "sources": ["repo:src/ctx/_proc.py", "repo:src/ctx/orchestrator.py"],
  "selection_note": "Process helpers and orchestration only; other launchers were not selected.",
  "worker": {
    "identity": "my-driver@exact-revision",
    "model": "exact-provider-model-id",
    "command": ["/absolute/path/to/my-semantic-driver"],
    "settings": {"temperature": 0}
  },
  "limits": {
    "partition_bytes": 16000,
    "max_calls": 32,
    "wall_seconds": 300,
    "call_seconds": 60,
    "max_output_tokens": 2048,
    "max_tokens": 131072,
    "max_cost_usd": 1,
    "reserve_cost_usd": 0.05
  }
}
```

```bash
ctx semantic prepare request.json
# Use the full blob: handle printed by prepare:
ctx semantic run blob:PLAN
ctx semantic show blob:PLAN
ctx semantic resume blob:PLAN
ctx semantic resume blob:PLAN --retry-failed
ctx get blob:REPORT
```

`prepare` and `show` make no model calls. `run` and `resume` share one implementation
and one persisted budget. The checkpoint is committed before a driver starts and
after every outcome. Successful partitions are reused only within this exact map;
failed or uncertain attempts require `--retry-failed` and another reservation.
There are no automatic retries. A per-plan lock refuses concurrent execution,
including attempts using different short forms of the same handle.

Changing source bytes, question, prompt/response contract, worker identity,
model, settings, limits, redaction policy, or the optional `sample` string creates
a different plan. Set a distinct `sample` for an independent stochastic repeat.
Resume reuses the original frozen evidence even when repository files change.
Changing the redaction policy requires preparing again. Inference reuse is
explicit replay of recorded results, not the deterministic evidence-plan cache.

The source selection accepts explicit `repo:` files, `blob:`, `snapshot:`, and
`run:...#stdout` or `run:...#stderr` handles in the current workspace. Directory
discovery and cross-workspace selection stay with the host. Repeated identical
handles are deduplicated; identical content at different paths keeps both labels.
Selection/byte counts describe those selected inputs, not independent corroboration.

## Worker protocol and SDK

The driver receives one UTF-8 JSON object on stdin:

- `schema: ctx.semantic.request/v1`, the question, and the versioned analysis prompt;
- `worker.identity`, `worker.model`, and `worker.settings` from the prepared request;
- `evidence`: an immutable `blob:` handle, source label, inclusive `start`/`end`
  line numbers, byte count, and the partition's text;
- `max_output_tokens` and a JSON Schema in `response_schema`.

The driver owns authentication, model selection, and the model call. It must
apply the requested output-token limit and return one JSON object on stdout:

```json
{
  "schema": "ctx.semantic.response/v1",
  "findings": [{
    "summary": "An inference about the supplied evidence",
    "support": [{"ref": "blob:<the supplied full sha256>", "lines": [1, 3]}],
    "counterevidence": []
  }],
  "unresolved": ["Inspect the caller's cancellation handler"],
  "usage": {"input_tokens": 1200, "output_tokens": 180, "cost_usd": 0.002}
}
```

`findings` and `unresolved` may be empty. Each finding needs support; support and
counterevidence must cite only lines in the supplied partition. The runtime also
validates byte/string limits, rejects unknown/duplicate fields and nonfinite
numbers, and preserves the raw bounded response when validation fails. Validation
checks citation membership and coordinates, **not whether a cited line proves the
claim**. Unresolved dependencies are suggestions for the host, never automatic calls.

Attach `usage` from the provider or harness envelope, never from generated prose.
`input_tokens` includes all input, including cache reads; `output_tokens` includes
all billed output/reasoning the provider reports. Aggregate any internal driver
calls and retries. Missing fields stay null/unknown. Driver identity, model identity,
and usage are declared by the driver and are not independently authenticated.

The SDK exposes the same operations without shelling out to the CLI:

```python
from ctx.semantic import prepare, run
from ctx.semantic.worker import WorkerResult
from ctx.store import Store
from ctx.workspace import resolve_workspace

ws = resolve_workspace(".")
store = Store(ws.workspace_id)
try:
    plan_handle = prepare(ws, store, request_dict)
    report_handle, report = run(ws, store, plan_handle)  # configured command driver
finally:
    store.close()
```

An embedding host can instead pass `worker=callback` to `run`. The callback takes
`request_bytes`, keyword arguments `timeout` and `response_bytes`, and returns
`WorkerResult(stdout=bytes, stderr=bytes, error=None, returncode=0)`. SDK requests
may omit `worker.command`; the declared identity, model, settings, and budgets
still apply. The callback must honor
the timeout, byte limits, and cancellation itself. The built-in command transport
enforces these at the owned-process boundary on POSIX systems. It starts no MCP
server and injects no workspace or editing tools. Its temporary working directory
is not an OS sandbox: use the host's sandbox when the driver is untrusted. Children
that deliberately escape their process group require external containment.

## What is bounded, and what is measured

| Limit | Default | Contract |
|---|---:|---|
| Selected source bytes | 4 MiB | Refuse oversized inputs before inference; maximum configurable 64 MiB |
| Partition bytes | 16,000 | Exact, nonoverlapping model-view coverage; prefer Python top-level/paragraph boundaries, then whole lines |
| Worker stdout / stderr | 64,000 / 16,000 bytes | Stop on overflow; retain the bounded prefixes and mark the attempt failed |
| Root calls | 32 | Worker attempts, including failures and retries; internal provider calls belong to the driver's accounting |
| Concurrency / depth | 1 / 1 | No recursive worker dispatch by straitjacket |
| Root / per-call time | 300 / 60 seconds | Remaining root allowance bounds each worker; unknown interrupted attempts retain their reserved duration |
| Output tokens | 2,048 | Passed to the driver for provider-side enforcement |
| Root token admission | 131,072 | Reserve serialized request bytes plus the output-token limit; reconcile upward with reported tokens |
| Root cost admission | $1, at $0.05 per attempt | Reserve before dispatch; reconcile upward with reported cost |

Cost and token admission are conservative estimates, **not provider billing
guarantees**. Reservations are not refunded when actual usage is lower. An
overrun stops further dispatch; it cannot undo a bill. Missing usage keeps the
reservation charged and the actual total unknown. `known_cost_usd` is only the
known subtotal. `reported_cost_usd` is null if any attempt lacks cost. No
provider-price lookup or model fallback occurs in this mechanism.

Root time survives resume as accumulated worker duration (or the reserved
duration of an uncertain attempt); storage/setup time can add overhead. Within
one invocation, bookkeeping time also reduces the remaining allowance. This is
not an end-to-end latency estimate; evaluate complete tasks separately.

Coverage separates successfully validated partitions/bytes from **selection
completeness**, which remains unknown even after every selected byte was processed.
Processing coverage records valid responses for assigned partitions; it does not
prove that a model attended to every line or understood it correctly.
Redaction and control sanitization produce their own immutable model-view blobs;
citations refer to that view and source records retain the raw provenance. No
transformed line number is presented as a raw-source coordinate.

The join is deterministic: preserve structured findings and counterevidence,
deduplicate byte-identical findings, and sort unresolved questions. It never
performs a hidden synthesis call or inserts probabilistic labels into the fact
store. Reports label all findings `model_inference` and expose partial coverage,
attempts, requests, outputs, root accounting, and evidence handles. Model-visible
CLI output uses the normal redaction and result-token budget; full results remain
addressable. Retention manifests explicitly keep the report's transitive evidence
and latest checkpoint alive under the existing store policy.

## Evaluation and next steps

The acceptance tests exercise frozen evidence, exact coverage, redaction, invalid
citations, cost/token reservations, interruption, resume, concurrent launches,
process cleanup, output floods, retention, and CLI execution with fixture drivers.
They establish mechanism behavior, not improved model accuracy or patch success.

Use the [four-arm evaluation protocol](../evals/SEMANTIC-MATRIX.md) before selecting
semantic mapping automatically. Host-native provider adapters, parallel dispatch,
adaptive follow-up under a shared parent budget, and persistent interpreters are
separate additions that require evidence of a benefit. The host still owns patch
application and the ordinary [edit verification loop](EDIT-LOOP.md).
