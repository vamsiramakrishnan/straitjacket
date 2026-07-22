# Use cases

Straitjacket is useful when a coding agent needs more evidence than its transcript should retain.

The relevant question is not simply whether an operation produces a large result. The question is whether the decisive evidence must remain recoverable after the first turn.

## Choose by task

| Task | Typical failure | Start with |
|---|---|---|
| Diagnose a noisy test suite | A long traceback hides the failure census | `ctx run -- pytest -q` |
| Investigate a repository | Intermediate searches and reads accumulate across turns | `ctx ask`, `ctx q`, or `ctx plan` |
| Run known verification steps | The model schedules each deterministic step in a separate turn | `ctx seq` |
| Execute a computed investigation | Branching and aggregation happen inside the transcript | `ctx eval` |
| Follow a long build | The agent polls and repeatedly absorbs partial logs | `ctx run --bg-after` and `ctx job` |
| Compare before and after | The model rereads two full results to infer a delta | `ctx diff` |
| Inspect a large connector response | A structured payload becomes permanent transcript history | Host interception and bounded retrieval |
| Delegate exploration | The parent receives either the full trace or an unauditable summary | Address-backed evidence and checkpoints |

## 1. Diagnose a noisy test suite

### Problem

A large test run can produce thousands of lines. Arbitrary truncation may hide the complete failing-test set. Showing only the first failure can hide a shared cause. Later compaction may remove the traceback that matters.

### Workflow

Capture the run:

```bash
ctx run -- pytest -q
```

Read the failure census before retrieving detailed tracebacks. Then retrieve only the active failure:

```bash
ctx get run:<id>#stdout --lines 418:472
```

Search the captured result when the relevant exception is known:

```bash
ctx search run:<id>#stdout "MissingTenantError" --context 3
```

Ask Straitjacket to diagnose captured failures without rerunning them:

```bash
ctx ask "Why did the test run fail?" --intent diagnose --run run:<id>
```

### Expected result

The model sees:

- the complete failure identity census;
- selected evidence for each failure;
- declared omission and coverage;
- exact addresses for tracebacks and output regions;
- one immutable run handle for later comparison.

### Do not use this pattern when

The complete test output is already small. Small results should pass through without creating unnecessary retrieval steps.

## 2. Locate a symbol and its use sites

### Problem

A model often lists files, searches a name, opens several candidates, then searches callers. Each step may be individually small while the accumulated exploration remains in the transcript.

### Workflow

Use a typed intent:

```bash
ctx ask "Where is AuthContext defined and used?" \
  --intent locate \
  --symbol AuthContext
```

Use direct commands when you already know the operation:

```bash
ctx def repo:src/auth.py:AuthContext
ctx refs AuthContext --path src
ctx callers AuthContext.resolve
ctx callees AuthContext.resolve
```

### Expected result

The model receives an organized set of definition and use-site identities with file and span coordinates. It does not need to retain the raw results of every intermediate search.

## 3. Estimate change impact

### Problem

A text search over a common symbol can create a large low-precision result. The model then manually infers which references are definitions, calls, tests, or unrelated text.

### Workflow

```bash
ctx ask "What could break if CacheKey.build changes?" \
  --intent impact \
  --symbol CacheKey.build \
  --depth 4
```

Or use the call graph directly:

```bash
ctx impact CacheKey.build --depth 4
```

### Expected result

The result should separate direct sites from bounded transitive reach and disclose the active analysis engine. Treat the output as structural evidence, not proof that every runtime path is covered.

## 4. Run known verification steps in one round

### Problem

The model already knows the commands it needs to run, but executes each command through a separate reasoning turn. Model latency surrounds deterministic work, and every intermediate output becomes context.

### Workflow

```bash
ctx seq \
  'git diff --stat' \
  'python -m pytest tests/unit -q' \
  'ruff check src tests'
```

Use `--keep-going` when later checks remain useful after a failure:

```bash
ctx seq --keep-going \
  'python -m pytest tests/unit -q' \
  'ruff check src tests' \
  'python -m mypy src'
```

### Expected result

Each step retains its own evidence identity. The model receives one combined bounded result instead of scheduling and parsing each command separately.

### Operating rule

Batch deterministic fan-out. Return to the model when new evidence can change the next action.

## 5. Execute a computed investigation

### Problem

The workflow requires loops, branching, parsing, or aggregation. A fixed command sequence cannot express it, but implementing the control flow through model turns is slow and context-heavy.

### Workflow

```bash
ctx eval investigation.py
```

Example:

```python
from pathlib import Path

files = list(Path("src").rglob("*.py"))
large = sorted(
    ((path.stat().st_size, path) for path in files),
    reverse=True,
)[:20]

for size, path in large:
    print(f"{size:>8} {path}")
```

### Expected result

The script and complete output remain addressable. Only the bounded final digest enters context.

### Security note

`ctx eval` is a capture boundary, not an operating-system sandbox. The script runs with the authority of the invoking user.

## 6. Follow a long-running build or integration test

### Problem

The agent waits, polls, rereads a growing log, or times out while the child process continues. Partial output repeatedly enters context and process state becomes implicit.

### Workflow

```bash
ctx run --bg-after 30 -- ./scripts/integration-test
```

Continue other work. Inspect the job later:

```bash
ctx job <job-id>
ctx job <job-id> --tail 100
ctx job <job-id> --wait
```

Stop the process group when required:

```bash
ctx job <job-id> --kill
```

### Expected result

The transcript contains stable lifecycle transitions and bounded tails rather than a polling conversation. A completed job finalizes into a normal `run:` artifact.

## 7. Compare verification runs

### Problem

After a change, the model rereads the complete before and after outputs and attempts to infer the behavioral delta.

### Workflow

Capture both runs:

```bash
ctx run -- pytest -q
# make the change
ctx run -- pytest -q
```

Compare the handles:

```bash
ctx diff run:<before> run:<after>
```

Or use the typed intent:

```bash
ctx ask "What changed between the verification runs?" \
  --intent compare \
  --run run:<before> \
  --against run:<after>
```

### Expected result

The comparison should identify changes in exits, failures, templates, signals, and stream sizes. New evidence should retain exact coordinates.

## 8. Review or verify a change

### Problem

A coding agent performs repository inspection, test selection, execution, and evidence joining as an open-ended series of tool calls. The boundary between observation and mutation is unclear.

### Workflow

Preview the compiled plan:

```bash
ctx ask "Review the current change" \
  --intent review \
  --command 'python -m pytest -q' \
  --plan
```

Execute it after reviewing the plan:

```bash
ctx ask "Review the current change" \
  --intent review \
  --command 'python -m pytest -q'
```

Use `verify` for a narrower verification workflow:

```bash
ctx ask "Verify the current change" \
  --intent verify \
  --command 'python -m pytest -q'
```

### Expected result

The plan declares its execution class. `review` and `verify` may run tests. `locate`, `impact`, `diagnose`, `trace`, and `compare` are observation-only.

## 9. Search or aggregate a large repository corpus

### Problem

A broad search returns thousands of text matches. The model needs a bounded file set, a census, or a grouped summary rather than every line.

### Workflow

Select files before scanning:

```bash
ctx q 'corpus --ext py --changed | outline'
```

Group references by file:

```bash
ctx q 'refs TokenBucket | group file | top 10'
```

Aggregate structured records from a captured artifact:

```bash
ctx q 'records run:<id>#stdout --jsonl | group level | count'
```

### Expected result

The pipeline carries typed records and coverage through each stage. The final view is bounded, and intermediate results remain addressable.

## 10. Inspect a large connector or MCP response

### Problem

Repository, cloud, browser, and MCP tools can return thousands of objects. The model often needs a count, schema, filtered subset, or one exact object rather than the complete serialization on every later turn.

### Workflow

Use the host integration so the post-tool gate can capture oversized responses. The bounded result should preserve:

- result count and coverage;
- stable object identities;
- schema or column information;
- exceptional or high-value rows;
- continuation handles for the complete payload.

Use `ctx get` or `ctx search` against the returned handle to recover exact objects or regions.

### Expected result

The transcript carries queryable identity and coverage, not permanent residency of the full connector payload.

## 11. Delegate exploration with evidence

### Problem

A parent agent either receives the sub-agent's entire trace or accepts a short summary with no way to verify it.

### Workflow

A delegated investigation should return:

1. a conclusion;
2. evidence handles with coordinates;
3. searches and checks performed, including negative searches;
4. unresolved claims labeled as hypotheses.

The parent can inspect any cited claim:

```bash
ctx get <handle>
```

Use a checkpoint when the result must survive a long task boundary:

```bash
ctx checkpoint \
  --goal "identify the authentication regression" \
  --state "root cause isolated" \
  --evidence "run:<id>#stdout failing traceback" \
  --evidence "snapshot:<id> AuthContext.resolve"
```

### Expected result

Delegation becomes bounded and auditable. The parent receives the result and its evidence addresses, not the fork's entire working context.

## Evaluate success correctly

Containment is useful only when it preserves task-relevant evidence.

Track both:

```text
containment ratio
  = 1 - model-visible tool-output tokens / captured tool-output tokens

evidence preservation
  = tasks solved with containment / tasks solved without containment
```

A smaller digest is not automatically better. The target is lower context residency at matched or better task success, with every omission declared and resolvable.

---

[Documentation](README.md) · [Getting started](GETTING-STARTED.md) · [CLI guide](CLI.md) · [Core concepts](CONCEPTS.md)
