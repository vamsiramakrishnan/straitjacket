# Investigate and verify a repair

Straitjacket can carry a bug investigation through evidence gathering, model
analysis, anchored repair, and independent verification. Your configured coding
agent supplies the reasoning. Straitjacket executes the declared operations,
keeps the evidence outside the conversation, and enforces one task allowance.

The implementation has two layers:

- **Reusable execution services:** `TaskRuntime`, the task ledger, registered
  evidence operators, command capture, edit transactions, and verification.
- **An optional investigation policy:** selects the next operation, asks the
  agent to interpret results, and decides when to attempt a repair or stop.

The policy contains no budget arithmetic, journal writer, file-search engine,
or alternative patch implementation. A normal evidence plan and a standalone
semantic map can use the same runtime without importing the policy.

## What evidence gathering means

Evidence is the result of a specific observation, with enough provenance to
inspect what supports a conclusion. It is not an automatically generated summary
of the repository, and it is not a claim that the agent has understood everything.

| Operation | Evidence produced | What it establishes |
|---|---|---|
| Discover eligible files | File identities and scope/omission metadata | Which files the selected scope exposes |
| Search a file or saved output | Bounded matches, retrieval handles and coverage | Where the literal query matched |
| Read a source span | Immutable raw/model-view blobs, line range and snapshot | The exact bytes the model was shown |
| Run a named probe | Command, stdout/stderr, exit status and source state | What that command observed in this checkout |
| Map a model over selected evidence | Findings, support, counterevidence and unresolved questions | A model's interpretation of the selected material |
| Verify an edit | Applied-file identities, witness identities and actual check runs | Whether the fixed checks passed against these bytes |

An address lets the agent retrieve omitted detail without rerunning the command.
Source/model-view separation preserves redaction provenance. Citation checks
establish membership and coordinates; they do not establish entailment. A fully
processed selection is not proof of complete repository coverage.

The initial investigation policy enables `evidence.discover`, `evidence.search`,
`evidence.read`, `semantic.map`, and `probe.run` from the ordinary plan-operator
registry. Existing symbol, call-graph, structural-search, and evidence-join
operators remain available to ordinary plans. Their presence in the registry
does not silently grant them to every policy.

## Prepare and run

First configure the exact ACP agent/model through the existing setup workflow:

```bash
ctx setup --host codex --acp --acp-model 'your-exact-advertised-model-id'
```

Prepare a task on a **clean, committed Git checkout**. The request fixes the
read scope, allowed edit targets, verification commands and their witnesses.
Use stdin so the request itself need not become an untracked repository file:

```bash
ctx task prepare - <<'JSON'
{
  "schema": "ctx.investigation.request/v1",
  "question": "Find and fix the cancellation regression reproduced by this test.",
  "host": "codex",
  "scopes": ["src", "tests"],
  "targets": ["src"],
  "witnesses": ["tests", "pyproject.toml"],
  "checks": [
    {"kind": "behavior", "argv": ["python", "-m", "pytest", "tests/test_cancel.py", "-q"],
     "timeout": 60, "failure_exit_codes": [1]},
    {"kind": "behavior", "argv": ["python", "-m", "pytest", "tests", "-q"], "timeout": 120}
  ],
  "probes": {
    "focused-test": ["python", "-m", "pytest", "tests/test_cancel.py", "-vv"]
  },
  "limits": {
    "max_calls": 32, "max_steps": 128, "max_rounds": 24, "max_repairs": 3,
    "wall_seconds": 600, "call_seconds": 60,
    "max_tokens": 500000, "max_output_tokens": 4096,
    "max_cost_usd": 3, "reserve_cost_usd": 0.10
  }
}
JSON

ctx task run task-IDENTIFIER
ctx task show task-IDENTIFIER
ctx task resume task-IDENTIFIER
ctx task resume task-IDENTIFIER --retry-failed
ctx task cancel task-IDENTIFIER
```

The command returns a report handle and retained worktree path. Dependencies
must be available to commands in that checkout; a local untracked virtualenv is
not copied into it. Use an absolute interpreter path when appropriate. Commands
that change source or verification witnesses are refused as stale observations.
Generated build/test output should be ignored by Git.

`witnesses` names the files that define independent verification, including test
configuration when relevant. Witness bytes are pinned before execution, checked
around command runs, and excluded from agent edits. Choose witnesses that cover
how your checks are actually configured; the runtime cannot infer a complete
specification from a test command.

A baseline behavioral failure must have one of the configured failure exit
codes. Timeouts, signals, output overflow and unexpected exit codes do not count
as reproduction. If all behavioral checks already pass, the task ends as
`not_reproduced`, with no patch-success claim and no model invocation.

After a verified repair, inspect its patch and apply it explicitly:

```bash
ctx get blob:PATCH
ctx task apply task-IDENTIFIER
```

Application requires the original checkout to remain clean at the pinned base.
The candidate is verified again for freshness before handoff. This command does
not commit or merge changes. The retained worktree remains available for review.

## How the controller works

1. Pin the task configuration, repository base and verification witnesses.
2. Create or reopen the task's retained worktree. Run the fixed baseline checks.
3. Give the agent a bounded observation set, older observation handles, and the
   enabled operator descriptions. Large evidence stays in the artifact store.
4. Validate and dispatch one registered operation. The agent can request further
   searches, reads, semantic maps and named probes as dependencies emerge.
5. Accept a repair proposal only after current semantic analysis and an exact,
   unredacted read of its edit spans. Apply through the normal edit transaction.
6. Run the fixed checks independently. Failed verification becomes a new
   observation for another investigation round. Passing checks produce a patch
   and a verification receipt tied to the applied bytes.

The parent policy adapts between rounds; each semantic map currently has one
worker at a time and depth one. This implements adaptive external-context
analysis with model subcalls. It does not provide an arbitrary-code REPL or
unlimited recursive agent spawning. Repairs use the existing anchored-edit
contract: replacements in existing text files. File creation, renaming and
binary mutation require a different registered mutation capability.

Static instructions and response schemas precede changing observations in model
requests. This preserves a reusable request prefix where the provider supports
prefix caching; no measured cache-hit improvement is claimed.

## One execution context

`ctx.task_runtime.TaskRuntime` is the shared SDK boundary. It owns:

- Immutable root limits and task bindings.
- Durable operation identities and reservations before dispatch.
- Results, trusted-driver usage, failure/uncertainty and named checkpoints in
  the existing task ledger.
- A nonblocking coordinator lock and cancellation signal.
- Namespaces for children that share the same root allowance.

`ctx.plan_exec.execute_plan(..., runtime=runtime)` and
`ctx.semantic.run(..., runtime=runtime)` use this boundary. So does
`ctx.execution.run_capture(..., runtime=runtime, operation_key=...)`.
`ctx.edit_verification.verify_edit(..., runner=...)` accepts that same captured
execution path. These APIs do not import the investigation controller.

```python
from ctx.task_runtime import TaskRuntime, ExecutionLimits
from ctx.plan_exec import execute_plan
from ctx.semantic import prepare, run

runtime = TaskRuntime.create(
    ws, store, goal="Investigate the regression",
    limits=ExecutionLimits(max_calls=20, max_cost_usd=2),
)
with runtime.active():
    execute_plan(ws, store, evidence_plan, runtime=runtime)
    semantic_plan = prepare(ws, store, semantic_request)
    report_ref, report = run(
        ws, store, semantic_plan, worker=my_worker, runtime=runtime,
    )
```

For a registered `semantic.map` node, pass an `EvidenceAccess` instance with
`worker_spec` and admitted source handles, plus `worker=...`, to `execute_plan`.
`probe.run` similarly uses caller-configured commands from `EvidenceAccess`.
Both operators are execute-class and are refused on the observation-only MCP
plan tier. Ordinary reads, searches and `ctx ask` never start model calls.

The existing orchestration ledger remains readable. Legacy `ctx orchestrate`
keeps its existing routing/recovery semantics; it does not acquire the new
reservation guarantees merely because it writes to the same ledger. The new
durable runtime is an explicit execution mode, available through `ctx task` and
the SDK. Migrating legacy orchestration requires adopting that contract too.

## Budgets and uncertainty

| Quantity | Accounting rule |
|---|---|
| Calls | All owned model-worker invocations: parent decisions and semantic subcalls |
| Steps | All journaled leaf operations, including commands, reads and mutations |
| Time | Observed operation duration; running/uncertain attempts retain at least their reservation; live bookkeeping counts too |
| Tokens | Reserve request bytes plus output allowance; reconcile upward from driver usage |
| Cost | Reserve before model launch; reconcile upward; missing actual usage remains unknown |
| Policy bounds | Maximum investigation rounds, probes and repairs, recorded in policy state |

Reservations are admission estimates, not provider billing guarantees. An ACP
agent may make internal model calls or retries that ACP does not report. The
ACP adapter therefore discards model-generated usage and reports actual usage
as unknown. Command/SDK drivers must aggregate their own internal calls and
report usage from a provider envelope. There is no model fallback or price lookup.

Owned subprocesses have time and output limits and process-group cleanup. SDK
callbacks must honor the timeout/cancellation contract; Python cannot forcibly
interrupt an arbitrary in-process callback safely. Synchronous bookkeeping and
edit diagnostics may add overhead. The runtime stops further dispatch when an
allowance is exhausted; it cannot undo provider spend or elapsed work.

A reservation or outcome that cannot be persisted is an execution error.
Unlike optional telemetry, the journal is required for this execution mode.
One coordinator may own a task at a time. Parallel dispatch within a task is
not yet advertised.

## Resume is an operation contract

| Journal state | Resume behavior |
|---|---|
| Completed operation | Reuse its immutable result under the same operation/input identity |
| Failed or uncertain model call | Require explicit retry and another reservation |
| Applied edit with a lost completion record | Compare recorded before/after bytes; recover the receipt if the whole edit is present |
| Partially applied or externally changed edit | Stop for review; do not blindly repeat or reset the checkout |
| Changed witnesses, task policy or redaction binding | Refuse continuation under stale assumptions |
| Completed repair | Revalidate the verification proof and return its patch without new model calls |

This resumes **Straitjacket task state**. The ACP adapter currently starts fresh
agent sessions with bounded saved evidence; it does not restore a previous
agent conversation, hidden reasoning, or provider cache state. It uses the
configured agent's authentication and exact model identity, supplies no MCP
editing tools, and refuses requested permissions. A temporary agent cwd is not
an OS sandbox; native agent containment still matters.

## Validation and limits of the results

The regression suite executes real local test commands and anchored edits with
fixture inference. It covers failed-repair feedback, interruption and resume,
shared budgets, journal failure before dispatch, concurrent coordinators,
output overflow, and recovery after an edit commits before its completion row.

These tests establish mechanics. They do not demonstrate higher accepted-patch
rates, lower model cost, or lower end-to-end latency. Evaluate those on paired
real tasks with the same model, check definitions and repository base. A passing
report is scoped to its declared checks, with selection completeness unknown.
