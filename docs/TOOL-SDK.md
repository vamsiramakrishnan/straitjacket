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
session timeout. The session timeout caps each command; it is not a shared task
budget. Use [Task execution](TASK-EXECUTION.md) for durable root reservations,
resumable investigations and shared budgets.

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
Session handles are in memory, so export them before discarding the session.
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

The Antigravity shim now uses this API. Its default `command-output` profile
replaces only command execution and adds `ctx_get`/`ctx_search`; native file tools
stay available. `--tool-profile full` also replaces file discovery and viewing.
`find_file` uses the corpus file-set operator so empty files are discoverable.
`--evidence-output` exports the session capsule. `--usage-input-semantics` labels
usage assumptions; default `unknown` preserves raw reports.

This requires a build containing the change. The historical `ctx-harness==0.39.0`
pin in the supplied benchmark cannot import this API until a new release ships.
See the [implementation receipt](../evals/tool-boundary-2026-10-12.md).
