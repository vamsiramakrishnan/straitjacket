# Use cases

straitjacket is most valuable when a coding agent must inspect more evidence than its
transcript can safely hold. The common thread is not “large output” by itself. It is
**large output whose decisive evidence must remain recoverable after the first turn**.

This page starts with the work, not the mechanism. Each pattern names the failure mode,
the smallest useful straitjacket verb, and the evidence that should cross back into the
model’s context.

## Choose by failure mode

| You are doing | The usual failure | Start with |
|---|---|---|
| Running a noisy test suite | The first failure crowds out the failure census | `ctx run -- pytest -q` |
| Inspecting many files | Every intermediate read becomes permanent transcript history | `ctx seq` or `ctx py` |
| Following a long build | The agent idles, polls, and repeatedly absorbs partial logs | `ctx run --bg-after …` + `ctx job` |
| Searching a large repository | Text matches create a low-precision wall of bytes | `ctx q`, `ctx map`, `ctx search` |
| Comparing verification runs | The model rereads two complete outputs to infer a delta | `ctx diff run:A run:B` |
| Delegating exploration | The parent receives an unauditable summary or the whole trace | `ctx-explorer` with cited handles |
| Calling verbose MCP tools | Connector payloads become transcript residency | host interception + artifact-backed digest |

## 1. A test suite fails loudly

### The failure mode

A large suite can print hundreds of thousands of tokens. Conventional truncation keeps
an arbitrary slice; “first failure only” hides whether five tests share one cause; a later
compaction may remove the exact traceback that mattered.

### The bounded path

```bash
ctx run -- pytest -q
```

A useful test digest should preserve:

- the complete failing-test identity census;
- one compact evidence line per failure when the budget permits;
- exact traceback/output coordinates;
- declared overflow with a continuation address;
- the full raw run as an immutable artifact.

Use retrieval only for the failure you are actively diagnosing:

```bash
ctx get run:8d8335db6848#stdout --lines 418:472
```

### Why it helps

The model reasons over the **shape of the failure set** before it commits to one
traceback. Detail remains one bounded page fault away.

### Do not use it when

The command is statically small and already returns the complete answer. straitjacket
should not turn a six-line unit-test result into a retrieval workflow.

## 2. Repository exploration fans out

### The failure mode

The model repeatedly lists files, searches names, opens candidates, searches callers,
then opens tests. Even when every operation is individually bounded, the transcript
accumulates the intermediate exploration state and pays for it again on subsequent
turns.

### The bounded path

Use `ctx seq` when the fan-out is known before execution:

```bash
ctx seq \
  --step 'rg -n "TokenBucket" src tests' \
  --step 'git diff --stat HEAD~1' \
  --step 'pytest -q tests/test_token_bucket.py'
```

Use `ctx py` when later operations depend on structured results from earlier ones:

```bash
ctx py investigation.py
```

Use `ctx q` when the intent is expressible as a total pipeline over typed records:

```bash
ctx q 'fails last | in-changed | group symbol | top 10'
```

### Why it helps

Deterministic fan-out executes beside the repository. The model receives one organized
evidence result instead of becoming the scheduler, parser, join engine, and state store
for every intermediate command.

### The operating rule

> Batch deterministic fan-out. Return to the model when new evidence can change the
> hypothesis.

One giant plan is not always better. A plan that efficiently investigates the wrong
hypothesis wastes less context and more time. Batch within an epistemic epoch; re-plan
at genuine uncertainty boundaries.

## 3. A build or integration test runs for minutes

### The failure mode

The agent waits, polls, rereads growing output, or times out while the child process
continues. Partial logs repeatedly enter context, and process lifecycle becomes implicit.

### The bounded path

```bash
ctx run --bg-after 30 -- ./scripts/integration-test
```

If the process outlives the foreground window, the command returns a `job:<id>` handle.
Continue working, then request a bounded tail:

```bash
ctx job <id>
ctx job <id> --wait
```

A finalized job becomes an ordinary run artifact, so the same search, retrieval, and
diff operations apply.

### Why it helps

Model latency is removed from the process critical path. The transcript records stable
state transitions rather than a polling conversation.

## 4. The conclusion is in the tail—or one anomaly is in the middle

### The failure mode

Many CLIs print progress first and conclusions last. Head-only truncation preserves
startup ceremony while dropping the result. Conversely, a rare operational anomaly may
occur once in the middle of 20,000 repetitive lines.

### The bounded path

```bash
ctx run -- ./service-load-test
```

The text profile keeps a head/tail window. Log-oriented profiles group recurring
templates and surface structurally rare lines. Omitted regions receive spans rather than
being discarded.

### Why it helps

straitjacket treats position and rarity as selection signals and keeps the underlying
bytes intact, so an omitted region stays retrievable instead of being lost.

## 5. You need the delta, not two transcripts

### The failure mode

After an edit, the agent reruns a command and manually compares two large digests or raw
outputs. Duplicate evidence dominates the context; the behavioral change is implicit.

### The bounded path

```bash
ctx diff run:8d8335db6848 run:5a67c9de0123
```

The useful output is a delta census:

- exit or signal changes;
- failures added, removed, or changed;
- log templates appearing or disappearing;
- stream-size changes;
- exact coordinates for evidence new in the second run.

### Why it helps

Verification asks a comparative question. A comparative operator should answer it
directly instead of asking the model to implement a diff in attention.

## 6. A sub-agent explores on behalf of the parent

### The failure mode

Inline delegation floods the parent. Summary-only delegation saves context but makes
claims unauditable.

### The bounded path

The `ctx-explorer` agent reports in checkpoint shape:

1. conclusion;
2. cited evidence handles and coordinates;
3. searches attempted, including negative searches;
4. unresolved claims explicitly labeled as hypotheses.

The parent can spot-check any claim:

```bash
ctx get <handle>
```

### Why it helps

Delegation becomes quarantine with provenance. The parent receives the result, not the
fork’s entire working memory, without accepting an evidence-free conclusion.

## 7. A connector returns a giant structured payload

### The failure mode

Repository, cloud, browser, and MCP tools can return thousands of objects in one call.
The model often needs a census, a filtered subset, or one exact object—not the entire
serialization on every later round.

### The bounded path

Intercept the result at entry, store the complete payload, and emit a shape-aware digest:

- schema or column census;
- result count and coverage;
- ranked exceptional rows;
- stable object identities;
- continuation handles for the complete result set.

### Why it helps

The transcript carries queryable identity, not connector payload residency.

## A useful success criterion

Containment is successful only when it preserves task-relevant evidence. Track both:

```text
containment ratio = 1 - visible tool-output tokens / raw tool-output tokens

evidence preservation = tasks solved with containment / tasks solved natively
```

A small digest with poor decisive-evidence recall is not an optimization. The target is
high containment **at matched or better task success**, with every omission in
a captured digest declared and resolvable while its artifact is retained.

---

[Getting started](GETTING-STARTED.md) · [CLI guide](CLI.md) · [Concepts](CONCEPTS.md) · [Why straitjacket](WHY-STRAITJACKET.md)
