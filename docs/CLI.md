# CLI guide

The CLI is organized around four operations:

1. **capture** an operation before it can flood;
2. **query** stored evidence without replaying raw output;
3. **resolve** an address to exact bytes or a bounded sub-digest;
4. **measure** what the harness changed in the session.

The command surface is larger than four verbs because different execution shapes need
different safety contracts. The mental model can stay small.

## Command chooser

| Need | Command | Why |
|---|---|---|
| One noisy command | `ctx run -- <command>` | One birth gate, one immutable run artifact |
| A shell pipeline | `ctx run --shell '<pipeline>'` | Captures the stream-shaped program as one operation |
| Known steps | `ctx seq …` | Per-step provenance without model round-trips |
| Computed control flow | `ctx eval <script>` | Branch, loop, aggregate; one bounded final digest |
| Long-running work | `ctx run --bg-after N -- …` | Returns a job handle instead of idling |
| Inspect a job | `ctx job <id>` | Bounded live tail and lifecycle control |
| Exact evidence | `ctx get <handle>` | Address in, exact bytes or bounded zoom out |
| Search stored evidence | `ctx search …` | Search artifacts without re-execution |
| Compose typed facts | `ctx q '<pipeline>'` | Total, bounded repository/evidence query algebra |
| Compare two runs | `ctx diff run:A run:B` | Behavioral delta instead of two complete outputs |
| Session scorecard | `ctx stats --session` | Wire residency, rounds, behavior and interventions |
| Cumulative savings | `ctx gain` | Containment savings by command family/verb |
| Replay histories | `ctx replay …` | Read-only counterfactual analysis over recorded sessions |

## Initialize a workspace

```bash
ctx init
```

This writes the workspace configuration and ignore policy. Commit the files when the
policy is intended to be shared; keep machine- or secret-specific exclusions local.

## Capture one command: `ctx run`

```bash
ctx run -- pytest -q
ctx run -- ruff check .
ctx run -- git diff --stat
```

`--` ends Straitjacket’s options. Everything after it is the child command.

A run has two products:

- the full stdout/stderr artifact and manifest;
- a deterministic digest selected by the detected profile.

The digest header contains the run handle. Use it for later retrieval, search, or diff.

### Shell syntax

Use shell mode only when shell semantics are part of the operation:

```bash
ctx run --shell 'rg -n "TODO" src | sort | head -200'
```

Prefer argv execution for a single command. It avoids quoting ambiguity and gives the
harness a clearer command identity.

### Background after a threshold

```bash
ctx run --bg-after 30 -- ./gradlew integrationTest
```

If the command finishes before the threshold, the result is identical to a foreground
run. Otherwise the transcript receives a `job:<id>` while output continues spooling to
the store.

## Inspect long-running work: `ctx job`

```bash
ctx job <id>
ctx job <id> --wait
ctx job <id> --kill
```

`ctx job` is a bounded observation surface, not `tail -f` routed into the transcript.
Finalized jobs resolve to ordinary `run:` artifacts.

## Execute declared steps: `ctx seq`

Use a sequence when the operations are known before execution and each step should keep
its own evidence identity.

```bash
ctx seq \
  --step 'git diff --stat HEAD~1' \
  --step 'pytest -q tests/unit' \
  --step 'ruff check src tests'
```

A sequence is preferable to several model-mediated tool calls because scheduling,
capture, and intermediate storage remain local. It is preferable to `ctx eval` when no
computed control flow is needed.

## Execute computed control flow: `ctx eval`

Use eval when a script must branch, loop, or aggregate structured intermediate results.

```bash
ctx eval investigation.py
```

The script itself is stored as an addressable artifact. Intermediate command output does
not enter the transcript; failures remain deterministic and retrievable.

`ctx eval` provides bounded capture, not OS isolation. Treat it as having the same
execution authority as `ctx run` until the broker security boundary ships.

## Retrieve exact evidence: `ctx get`

```bash
ctx get run:<id>#stdout --lines 120:180
ctx get blob:<id>
ctx get <span-id>
```

Small regions return exact bytes. A region too large for the retrieval budget returns a
bounded zoom digest with further spans. Retrieval cannot recursively re-flood the
transcript.

A handle is an address today. It becomes an authorization capability only in the
broker-era design; do not present current content identifiers as a sandbox boundary.

## Search captured artifacts: `ctx search`

Use search when the evidence already exists in the store:

```bash
ctx search 'MissingTenantError'
ctx search 'authorization failed' --run run:<id>
```

Searching an artifact is cheaper and more trustworthy than rerunning a command merely
to recover text the harness already captured.

## Compose evidence: `ctx q`

```bash
ctx q 'fails last | in-changed'
ctx q 'refs TokenBucket | group file | top 5'
ctx q 'fails last | shared-cause | top 10'
```

`ctx q` operates over typed record streams such as failures, symbols, files, and sites.
The algebra is deliberately total: bounded stages, no loops, no recursion. This makes
costs statically boundable and every stage’s result addressable.

Use `ctx eval` when the control flow is genuinely computational. Use `ctx q` when the
intent is a bounded composition of repository and evidence facts.

## Compare runs: `ctx diff`

```bash
ctx diff run:<before> run:<after>
```

The comparison should answer the verification question directly: what failures,
templates, exits, signals, or stream sizes changed? New evidence receives coordinates.

## Measure a session

```bash
ctx stats --session
ctx gain
```

Read the scorecard in this order:

1. **task outcome** — containment is irrelevant if the task regressed;
2. **wire residency** — what actually crossed into context;
3. **rounds and repeated commands** — whether the harness removed control-loop churn;
4. **retrieval landings** — whether the reader followed evidence addresses;
5. **interventions** — whether steering fired, and on which measured condition.

`ctx gain` is an accounting view, not a quality score. Pair savings with evidence
preservation and task success.

## Run a host under the harness

### Claude Code

```bash
ctx wrap claude --proxy -- -p "fix the failing tests"
```

The wrapper injects host settings for the session and removes them when the process
ends.

### Antigravity

```bash
ctx antigravity install
```

The plugin is persistent. Both hosts use the same artifact store, digest contracts, and
retrieval vocabulary.

## Failure semantics

Straitjacket distinguishes safety from optional intelligence:

- safety gates fail closed when allowing an operation could violate the containment
  invariant;
- optional extractors, indexes, and accelerators fail open to a labeled lower-precision
  mode;
- degraded precision is disclosed in the digest;
- omission is declared and addressed, never silent.

## Output discipline

A good command result answers five questions:

```text
What happened?
What evidence supports it?
What was omitted?
How complete is the view?
What exact address retrieves the next useful detail?
```

That shape is the CLI’s real compatibility contract. Renderers and backends may evolve;
addressability, bounds, determinism, and declared coverage may not.

---

[Use cases](USE-CASES.md) · [Getting started](GETTING-STARTED.md) · [Concepts](CONCEPTS.md) · [Profile authoring](WRITING-A-PROFILE.md)
