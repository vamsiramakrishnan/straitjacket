# straitjacket

**Run noisy tools. Give the coding agent the result and a way to inspect the evidence.**

straitjacket captures command output in a local artifact store and returns a
bounded, deterministic digest. The agent can search the complete output or
retrieve a cited region without loading the whole log into its conversation.

**Keep using your coding agent.** Claude Code, Codex, and Antigravity remain
supported. Straitjacket adds capture, bounded retrieval, and an optional verified
edit workflow to the agent you choose; you keep its interface, models, login,
and permissions. Any terminal-capable agent can use the `ctx` CLI.

v0.38.0 · Python 3.11+ · package `ctx-harness` · command `ctx` · pre-1.0 · Apache-2.0

[Start here](docs/GETTING-STARTED.md) · [Choose a workflow](docs/USE-CASES.md) ·
[CLI reference](docs/CLI.md) · [Documentation site](https://vamsiramakrishnan.github.io/straitjacket/)

## Capture your first result

From a repository with a test suite:

```bash
python -m pip install --upgrade ctx-harness
ctx run -- pytest -q
```

No host integration is required for this command. A failed test command remains
a failed command. straitjacket changes how its evidence reaches the agent.

A large pytest result identifies failing tests and provides a `run:` address.
Use the address and coordinates printed by your run:

```bash
ctx get run:<id>#stdout --lines 120:180
ctx search run:<id>#stdout "AssertionError"
```

`<id>` is a placeholder, not a checked-in sample you can retrieve. The stored
artifact contains the complete captured stream. Retrieval is bounded too: a
large request returns a smaller region with a continuation.

When that loop is useful, connect the host you already use:

```bash
ctx setup
ctx doctor
```

Setup preserves unrelated settings and reports the changes it makes.
[Installation, host setup, and removal](docs/GETTING-STARTED.md).

## Choose the work you want to improve

| Job | Use | What you can inspect afterward |
|---|---|---|
| Diagnose a long test run, build, or log | `ctx run -- <command>` | Outcome, typed digest, complete stdout/stderr |
| Find the relevant symbol before reading files | `ctx map`, `ctx def`, `ctx refs` | Bounded structural views and source coordinates |
| Compare a failure with the next attempt | `ctx diff run:A run:B` | Changes in outcomes and evidence |
| Run a known sequence of checks | `ctx seq`, `ctx q` | Locally composed results with evidence addresses |
| Edit the source you actually observed | `ctx edit replace`, `ctx edit verify` | Sealed plan, apply receipt, checks tied to file hashes |
| Continue a task across workers | `ctx orchestrate`, `ctx task show` | Persisted task state, attempts, recovery, and budget records |

Start with capture and retrieval. Add editing or orchestration when the task
needs their contracts. See the [verified edit loop](docs/EDIT-LOOP.md) for
snapshot-based replacements, typed recovery, and prewalk handoffs.

## Why preserve the output outside the prompt?

A build log can outlive the decision it supported. Once pasted into the
conversation, it can occupy context on every later turn. Truncation removes
bytes the agent may need later; a free-form summary cannot provide the exact
original evidence.

straitjacket retains those bytes and gives the prompt a smaller view. Typed
profiles keep test identities, diagnostic locations, unusual log events, or
JSON structure according to the output family. They declare omissions and
provide retrieval addresses. There is no model call in the digest path.

Stored `run:`, `blob:`, and `snapshot:` handles identify immutable content while
retained. Live `repo:` addresses can carry a content anchor: reads verify the
span, relocate unchanged content, or refuse stale evidence.

[Follow one command through the system](docs/HOW-IT-WORKS.md).

## Decide whether to use it

| Your workload | Starting choice |
|---|---|
| Large outputs early in a long investigation | Capture them and retrieve only the evidence needed next |
| Repeated repository navigation | Use structural queries; watch retrieval count as well as token volume |
| Small output in a short task | Keep native execution; wrapping can add overhead |
| Need predictable local behavior without another model call | Use the CLI directly |
| Need transparent interception | Check the host matrix below before adopting |

Measure task completion, total usage, elapsed time, and retrieval calls together.
Compression alone does not establish a better coding outcome.

## Evidence, including the losses

The repository publishes fixtures, harnesses, raw records, and negative results
in [`evals/`](evals/). Two mechanism tests establish the core behavior:

- A deterministic 20,001-line log contains one quiet target at line 14,238 and
  two loud `ERROR` controls. Raw delivery uses 302,628 `o200k_base` tokens.
  straitjacket emits 531, retains the quiet target, and emits an address for the
  omitted region. This fixture checks address emission, not a retrieval round
  trip. See the
  [field-needle receipt](evals/field-needle-2026-07-20.md).
- Across 1,920 repository-span resolutions after edits, anchored addresses
  relocated 1,452 times, verified in place twice, refused 466 times, and returned
  the wrong content zero times. See the
  [anchor-drift receipt](evals/anchor-drift-2026-08-20.md).
- The task-ledger replay exercises persisted orchestration state across resume,
  typed recovery, and budget reconciliation. See the
  [task-ledger receipt](evals/task-ledger-replay-2026-09-02.md).

Those results test delivery and addressing. They do not prove that every agent
task becomes cheaper or faster.

The live agent referee records the counterexample. On three small canary tasks,
with one repeat per task, both native and contained arms solved 3/3, but the
contained arm used more input tokens, cost more, and took longer. In one
navigation-heavy dogfood run with a 40-turn cap, the native arm reproduced
eight failing test nodes and the wrapped arm reproduced five. That comparison
tests the full wrapper bundle, not containment alone, and its model field is
unrecorded. The wrapped arm made 210 retrieval calls; without a retrieval
ablation, those calls are a plausible contributor rather than a proven cause.
See the
[agent-harness results](evals/agentbench/RESULTS.md).

That boundary matters. straitjacket is most useful when output is large, early,
repeated, or likely to survive many turns. A short task with small, hot-cached
results may be better left native. The target is matched task success with less
context residency—not the highest compression ratio.

## Host support

| Entry point | Current behavior |
|---|---|
| Direct `ctx` CLI | Capture and retrieval without a host plugin |
| Claude Code | Command rewriting and oversized-result substitution on supported hooks |
| Codex | Implemented and contract-tested gates; live CLI receipt pending |
| Antigravity | Deny recognized floods and supply a bounded replacement command; no output substitution |

`ctx setup` merges JSON where safe and refreshes ctx-managed blocks. For
user-owned Codex TOML it prints a reviewed snippet rather than replacing the
file. See [host capabilities](docs/HOST-CAPABILITIES.md) for exact coverage.

## Boundaries

straitjacket is not:

- a process sandbox—commands retain the invoking user's authority;
- agent memory—it contains current tool evidence rather than choosing which old
  memories to recall;
- a free-form summarizer—digests are deterministic, typed, and addressable;
- a reason to wrap every command—the native path is often right for small work.

The local store contains captured command output as plaintext. Thirty days is
the default GC eligibility horizon; artifacts remain until `ctx gc` runs.
`ctx gc --retention-days N` can override the horizon, while pins and active
checkpoint leases survive collection. Treat the store with the same care as
the source and logs it captures. At-rest encryption is not implemented today.

The project is pre-1.0. Command and profile contracts may still change between
minor releases.

## Read next

| Next step | Guide |
|---|---|
| Install, configure, or remove a host integration | [Getting started](docs/GETTING-STARTED.md) |
| Apply and verify an observed edit | [Edit loop](docs/EDIT-LOOP.md) |
| Compare native, anchored, and prewalk strategies | [Paired evaluations](evals/EDIT-MATRIX.md) |
| Tune budgets and retention | [Configuration](docs/CONFIGURATION.md) |
| Resolve an error | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| Understand modules or contribute | [Architecture](docs/ARCHITECTURE.md), [Contributing](CONTRIBUTING.md) |
| Browse the full reference | [Documentation map](docs/README.md), [Changelog](CHANGELOG.md) |

## Development

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e '.[dev]'
pytest
```

New mechanisms need deterministic output, explicit degradation, and a named
evaluation gate. Follow [CONTRIBUTING.md](CONTRIBUTING.md).
