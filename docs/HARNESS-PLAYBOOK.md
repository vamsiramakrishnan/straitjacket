# Applying the Harness Playbook to straitjacket

The [Harness Playbook](https://stencil.so/blog/harness-playbook), published by
Can Bölük on September 2, 2026, argues for centrally owned state, execution
limits, compatibility rules, and a small permanent tool surface. Its distinction
between complete execution data and model-visible projections is especially
relevant here. Some of its replacement architecture is still being developed;
its examples are design evidence, not a performance forecast for straitjacket.

This assessment is based on straitjacket at `978036c`. The priorities below are
our proposed application of those principles. The first commit implements the
cancellation correction. Follow-on commits implement the [verified edit loop](EDIT-LOOP.md),
including receipt-gated prewalk, structural expansion, and paired outcome gates.
The capture, gateway, composition, and replay proposals below remain future work.

## What already exists

| Requirement | Existing mechanism | Remaining boundary |
|---|---|---|
| Keep evidence outside the transcript | `execution.run_capture`, immutable blobs, bounded digests and retrieval | Delegated host output has a separate capture path |
| Keep the permanent tool surface small | `mcp.TOOL_SCHEMA`: one tool with an operation discriminator | The surface gateway dynamically reveals backend schemas |
| Compose work before emitting | `ctx py`, `ctx q`, evidence plans | Calling the CLI from Python returns presentation text; it is not a typed data API |
| Recover orchestration state | `taskledger.task_state` folds persisted rows | This is task replay, not complete host-session or filesystem rewind |
| Bound long-running work | Foreground timeout, background jobs, orchestration wall and idle deadlines | Interruption previously escaped process cleanup |

## First change: cancellation owns cleanup

`wait_or_kill` previously cleaned up only after `TimeoutExpired`. A
`KeyboardInterrupt`, `SystemExit`, or other exception from the wait left the
separately launched process group alive. `run_capture` could then remove its
spools while the command continued running. The orchestrator had the same gap
in both its `communicate` and inactivity-monitor paths.

`kill_and_reap` now owns termination of the process group and reaping of the
leader. Both runners invoke it on exceptional exit and propagate the exception.
The inactivity path drains its readers before attaching partial output to a
timeout exception. Pipe handles close when the readers have stopped.

The regression gate is
[`tests/test_process_cancellation.py`](../tests/test_process_cancellation.py).
It launches real child/grandchild processes, injects three interruption types,
and checks four entry paths: the shared waiter, capture, wall-only orchestration,
and orchestration with inactivity detection. It checks descendant termination,
leader reaping, preservation of the original exception, and pipe closure.

This does not provide OS isolation. Descendants can escape a process group;
an owner killed with SIGKILL cannot execute Python cleanup. Background-job
persistence is separate from cancelling a foreground command. This change
also does not add a task-wide cancellation protocol for orchestration workers;
it handles exceptions delivered to their execution waits.

## Next changes, in order

### 1. Bound delegated capture before allocation

`orchestrator._run_bounded` uses `communicate()` without an inactivity deadline
and lists of byte chunks with one. Both accumulate complete output in memory.
A bounded final checkpoint does not bound that earlier allocation.

Move both branches to one spool-based capture mechanism. Retain complete output
as artifacts, parse usage and final results incrementally, and impose an explicit
capture quota. A quota breach must stop the job and declare incomplete capture;
it must not masquerade as a complete artifact. Disk spooling alone is not a
storage bound.

Gate: a deterministic flood on either stream stays within a measured memory
ceiling; quota termination retains an addressed partial artifact; usage parsing
and final results agree across both timeout modes.

### 2. Give the gateway a stable invocation mode

`Gateway.visible_tools` changes when families are revealed. The existing
single-tool retrieval server already demonstrates a fixed schema; the gateway
needs an equivalent optional mode for external capabilities.

Separate backend schema discovery from invocation. Return a requested schema
as bounded data and invoke through a fixed entry point. Preserve backend identity,
family restrictions, and approval context. A generic dispatcher must not erase
the distinction between observing and mutating operations.

Gate: the serialized `tools/list` response stays byte-identical through
discovery and calls; hidden families remain inaccessible; host contract tests
verify that approvals still identify the actual backend operation. Measure
cached input, latency, and task success before changing the default.

### 3. Preserve gateway omissions as evidence

`surface_gateway._bound_result` currently truncates each text block separately.
Many blocks can exceed the nominal cap in aggregate, structured/binary data
passes through, and omitted text has no retrieval address.

Store oversized supported results before rendering a response-wide bounded
projection. Keep diagnostics separate from backend data, and apply redaction
on retrieval. Define handling for unsupported media explicitly.

Gate: multi-block and structured floods respect the aggregate delivery budget;
every supported omitted portion resolves to retained evidence; policy-protected
bytes do not leak through the new retrieval path.

### 4. Expose typed composition over artifacts

Build a supported Python interface around capture manifests and streaming
artifact readers. Code should parse complete JSON or iterate full records,
then request a bounded projection only at emission. It should not parse a digest
to reconstruct the result. Keep raw access within the existing local trust
boundary; it is not a new unredacted MCP operation.

Gate: an aggregation finds a target beyond the display budget, matches the raw
reference result, emits once, and retains source handles. Include large records,
invalid UTF-8, and redaction-changing retrieval cases.

### 5. Define exactly what replay restores

Keep the task ledger's existing reducer. Inventory decision inputs outside it:
resolved configuration, selected capabilities, policy versions, and pending
work. Persist the inputs needed to reproduce scheduling decisions. Distinguish
restoring decisions from rerunning effects; never relaunch a completed mutation
merely because a session was reopened.

Gate: restart with changed defaults reproduces recorded decisions or explicitly
requires migration; interrupted attempts remain distinguishable from completed
ones; checkpoints survive the retention lifecycle.

## Evaluation rule

Use matched workloads and report task success, total model input, cached input,
retrieval rounds, elapsed time, peak memory, and retained bytes. Separate schema
changes, capture changes, and delivery changes into ablations. The existing
[agent-harness results](../evals/agentbench/RESULTS.md) already show that wrapping
small tasks can cost more. Compression ratio alone cannot select a default.
