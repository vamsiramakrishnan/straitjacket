# Embed Straitjacket at the tool boundary

Use `ctx.sdk.ToolSession` when your harness owns its Python tools. Use
`ctx run --json` when the harness lives in another interpreter or language.
Both return command termination explicitly and keep complete captured streams
addressable. Neither requires a provider SDK or calls a model.

## Capture, then retrieve

```python
import asyncio
from ctx.sdk import ToolSession

async def investigate():
    session = ToolSession(".", timeout=600, max_tokens=1200)
    result = await session.run(["pytest", "-q"])
    print(result.to_json())
    if result.command_exit_code != 0:
        failures = await session.search(result.ref, "FAILED|AssertionError")
        print(failures)
    await session.export("task-evidence.tar")

asyncio.run(investigate())
```

`run` accepts argv or an explicit Bash command string. Prefer argv for a single
executable. `working_directory` is confined to the session workspace, including
symlinks. This embedding boundary always keeps one root, even if the CLI policy
permits outside reads. `timeout_seconds` must be finite, positive and no greater than the
session timeout. Without durable execution this caps each command. With
`durable=True`, commands, reads, plan leaves and repair checks share the existing
`TaskRuntime` allowance; see [Durable sessions](#durable-sessions).

The caller controls authentication, tool registration and permission decisions.
Commands retain the caller's OS authority. Directory confinement does not sandbox
an executable. This API adds no command-execution operation to the bounded MCP
server.

## Read the termination fields

| Field | Meaning |
|---|---|
| `command_exit_code` | Child's exit code; null when it died by signal |
| `signal` | Signal name, including the signal used during timeout/cancellation |
| `timed_out` | Capture deadline expired |
| `cancelled` | Caller requested cancellation while the command was running |
| `output_limited` | Durable capture reached its byte cap, when true |
| `ref` | Full `run:` address, suitable for retrieval or capsule export |
| `delivery` | `inline` for complete small text, otherwise `digest` |
| `stdout`, `stderr` | Separate sanitized text streams in inline mode |
| `digest` | Bounded profile output in digest mode |
| `redactions` | Named redaction rules applied to inline text, when present |

The Python `native_exit_code` property maps signal termination to `128 + signal`
and timeout to `124`. It is distinct from `command_exit_code`. Launch failures
raise `ExecutionError`; an executable that never started has no command exit code.
Invalid requests raise before launch.

A subprocess integration receives the same successful-result schema:

```bash
ctx run --json -- pytest -q
```

Exactly one JSON object is printed for a captured command. CLI process status
remains `0` for child success, `3` for child failure/signal and `124` for timeout.
A missing executable returns `127` with `error: launch_failed`, a null reference
and null command status. `--json` rejects `--bg`, `--bg-after` and `--passthrough`
before execution. Argument/workspace errors retain the normal CLI error path.
The wire schema is [command-result-v1.schema.json](../spec/schemas/command-result-v1.schema.json).

## Choose delivery without losing evidence

`delivery="auto"` inlines only complete text streams inside the configured
byte and line limits **and** the result budget after JSON serialization and
sanitization. Empty output remains a complete result. No newline is appended to
either captured stream. ANSI/control stripping and redaction apply to the view;
raw stored bytes remain unchanged. Binary streams, timeout/cancellation and large
or expensive-to-escape output use existing profile digests and delivery policy.
`delivery="digest"` forces that path.

Digest content uses the shared resolver, failure budget, engagement filter and
bounded backstop. The JSON result is also checked after escaping. Its wire cap is four times the
result-token budget in bytes, with a 512-byte floor for status and a retrievable
omission notice. Budgets
are estimated bytes-to-token limits, not a provider tokenizer guarantee. Wrapping
small commands still adds capture and schema overhead; measure total task behavior
before choosing to wrap every tool.

## Compose retrieval

```python
text = await session.get(result.ref + "#stderr", start_line=20, end_line=50)
hits = await session.search(result.ref, "warning:", context=3, fixed=True)
view = await session.query("refs Foo | group file | top 3 | get --context 5")
```

`retrieve(op, ref=..., selector=..., patterns=..., options=...)` exposes the typed
observe operations used by MCP. It cannot run commands, edit files, send messages
or change the workspace. `query` uses the existing finite query grammar; a quoted
pattern remains one pattern. Large reads and searches declare omissions and
continuations. Each worker owns its SQLite connection; concurrent calls never
mutate process-wide cwd, environment or another call's budgets. Use `env={...}`
for child-only environment overlays. Values are not recorded in manifests or
telemetry.

## Cancel and move evidence

Cancelling the coroutine signals capture, kills its owned process group and
waits for cleanup before propagating `asyncio.CancelledError`. Partial streams
are published when capture started; their handle remains in `session.handles`.
Repeated caller cancellation cannot detach that cleanup worker. Processes that
intentionally escape the group with `setsid()` remain outside this guarantee.
Retrieval workers finish their existing bounded operation before cancellation
propagates; they do not have the command transport's interruption points.

`session.export(path)` uses the existing capsule closure over observed immutable
handles. Import it with `ctx capsule import`. The capsule contains raw evidence
and invocation metadata; treat it as captured logs. Mutable repository paths
become portable only when a retrieval has actually snapshotted their content.
Non-durable session handles are in memory; export them before discarding the session.
Artifact retention follows normal store policy.

## Measure delivered bytes and preserve provider uncertainty

`audit/tool-calls.jsonl` records operation, producer, elapsed time, actual
serialized delivery bytes, captured bytes for runs, typed termination and
expansion. It records no commands, patterns, output or environment values.
Existing `ctx gain` telemetry receives the actual command envelope size, once,
instead of crediting a digest that was never delivered. Bytes are not provider
usage or billed spend.

`ctx.tooltelemetry.usage_record` preserves SDK prompt, cache, candidate, thinking
and total counters. Input semantics are `unknown` by default. Set `includes_cache`
or `excludes_cache` only when the provider path establishes that relationship.
Missing counters remain null; invalid or contradictory counters are declared.
No price card or billing assumption is inferred.

## Compact model delivery

`await session.run_model(argv)` returns a compact text view with a canonical
run address, explicit termination and separate stdout/stderr labels. It records
the bytes of that view. `await session.run(argv)` still returns `CommandResult`;
`to_json()` retains the machine schema and `to_model()` renders its compact view.
Rendering a returned object yourself does not retroactively change telemetry.
Both views are sanitized and budgeted. Any further body cut declares omitted
bytes and a continuation. The model view has the same 512-byte metadata floor
as the JSON view. Complete captured streams remain available at the run address.

## Navigate and compose

```python
small = await session.navigate("src/config.py")
outline = await session.navigate("src/large.py", mode="outline")
body = await session.navigate("src/large.py", symbol="Parser.parse")
span = await session.navigate("src/large.py", start_line=40, end_line=60)
```

Auto mode reads a file of at most 200 lines directly; larger files get the
existing cached skeleton, with parser disclosure and symbol coordinates.
An explicit range or symbol requests its body directly. Ambiguous or absent
symbols refuse with a navigation hint. Snapshot capture is bounded to 4 MiB;
larger/binary artifacts remain available through ordinary retrieval selectors.
Snapshots keep old evidence stable after edits. Navigation introduces no initial
context pack and no prerequisite outline for a requested range.

`session.query(pipeline)` composes the existing finite query algebra in one
bounded response. `session.investigate(plan)` runs an observe-only `ctx.plan/v1`
plan through the existing executor. Its leaf operations share a durable root's
step allowance. Execute-class plan operators remain rejected on this surface.
Query/plan limits, aggregate budgets and addressed omissions still apply.

## Durable sessions

```python
from ctx.task_runtime import ExecutionLimits

session = ToolSession(".", durable=True,
    limits=ExecutionLimits(max_steps=200, wall_seconds=900, call_seconds=120))
result = await session.run(["python", "-m", "pytest", "-q"], operation_key="reproduce-1")
task_id = session.task_id
# After a process restart:
resumed = ToolSession(".", task_id=task_id)
same = await resumed.run(["python", "-m", "pytest", "-q"], operation_key="reproduce-1")
assert same.ref == result.ref
```

Each worker owns a store connection and acquires the existing task execution
lock. Calls within a durable session serialize; other coordinators receive
`task_already_running`. Non-durable command calls can still run concurrently.
Named completed operations replay their immutable result. Reusing a name with
different inputs refuses. Use a new name for a new probe after an edit. Automatic
names identify new calls; they cannot establish exact replay after a host restart.
Limits are immutable on resume. Default `ExecutionLimits` allow 128 steps,
300 seconds total, 60 seconds per call and 256 KiB of capture per command.
Capture-limit, timeout and cancellation flags remain visible.

Commands and edit applications have mutation authority. An interrupted operation
with unknown outcome blocks further mutations, even with `retry_failed=True`.
Reconcile it through the existing `TaskRuntime.perform(..., reconcile=...)`
contract before continuing. Reads can continue while budget remains. Cancellation
waits for owned command cleanup and persists partial evidence. This session
accounts for tool work; the caller must also route model invocations through
TaskRuntime to share model-call/token/cost reservations.

Handles persist in the `tools.session` checkpoint and remain exportable after
restart. To enable read reuse, explicitly supply `context_epoch="host-context-1"`.
Complete delivered lines are tracked by coordinate and content, with their
source snapshot. Subsequent reads emit changed and unseen lines; previously
delivered lines are counted and have a full-read continuation. Insertions may
cause conservative re-emission. Truncated or transformed coordinates receive no
reuse credit. Memory eviction also causes re-emission.

Call `session.set_context_epoch(new_epoch)` whenever model context is lost,
compacted or restored differently. `fresh=True` forces a complete read. A resumed
session reuses delivery state only with the same explicit epoch. Without an epoch
signal, reads always return full content: the library cannot infer model memory.

## Repair with evidence

`session.repair(failure_ref, request=anchored_request, targeted=[Check(...)],
regression=[Check(...)], witnesses=[...], operation_key="repair-1")` connects a
completed failing run, an existing CAS edit transaction and exact-byte checks.
Alternatively pass `edit_receipt=receipt_ref` for an already applied transaction.
These two inputs are exclusive. Check and witness validation precedes mutation.
The first targeted behavioral check must rerun the failing argv; checks use the
failure's working directory and the session's child environment overlay.

The returned `ctx.repair-verification/v1` object names the failure, applied edit,
verification proof, input hashes and targeted/regression roles. It stops on a
failed check or changed inputs and never counts unrun regression checks as
passing. Passing proofs are revalidated by `validate_verification` at handoff;
changing an edited or witness file makes them stale. Witness paths are explicit:
this is evidence for the selected checks and inputs, not a whole-repository proof.
The SDK adds no edit/command capability to bounded MCP.
`repair_model(...)` returns a bounded summary with the full proof address and
records its actual delivery size. The default integration's `ctx_repair` accepts
one observed anchored span and explicit targeted/regression shell commands; it
has the same local mutation/command authority as `run_command`.

## Antigravity integration and evaluation

The Antigravity shim uses compact delivery by default. Its `command-output`
profile replaces command execution and adds `ctx_get`, `ctx_search`, `ctx_repair`,
`ctx_navigate`, `ctx_query` and `ctx_investigate`; native file tools stay available.
`--tool-profile full` also replaces file discovery and viewing.
`find_file` uses the corpus file-set operator so empty files are discoverable.
`--evidence-output` exports the session capsule. `--usage-input-semantics` labels
usage assumptions; default `unknown` preserves raw reports.

The shim opens a durable tool task (4,096 steps, the agent wall deadline and
8 MiB capture per command). `--task-id` reopens its journal, without claiming
to restore the provider conversation. Reuse stays off unless the host supplies
`--context-epoch` and honors context resets. `--result-format json`,
`--no-navigation` and `--no-composition` provide isolated ablations.
Reported custom tool calls and delivery bytes exclude native builtins.
`model_requests` and `all_tool_calls` remain null when the provider does not
expose those observations; aggregate usage is not a turn count.

The 0.40.0 release candidate contains these APIs; the historical published
0.39.0 benchmark pin does not. The [matched evaluation runner](../evals/WORKFLOW-ABLATION.md)
pins the wheel contents, provider SDK, model, corpus and task commits before
live work. [Model-free receipts](../evals/workflow-2026-10-18.md) establish local
delivery and evidence behavior; live cost/quality/turn reductions remain unmeasured.
