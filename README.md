# straitjacket

Keep large tool output out of your coding agent's prompt. Keep the evidence available.

**v0.35.1 · pre-1.0 · Python 3.11+ · Apache-2.0**

straitjacket is a local context-containment harness for Claude Code, Codex, and
Antigravity. `ctx run` captures command output directly; supported host paths
can route recognized, potentially unbounded output through the same boundary.
For each captured result, the agent receives a small deterministic digest and
bounded routes back to the stored bytes.

## Try it

```bash
python -m pip install --upgrade ctx-harness
cd your-repository
ctx setup
ctx doctor
ctx run -- pytest -q
```

The package is `ctx-harness`. The command is `ctx`.

## Why it exists

A tool result is not only an output event. Once it enters an agent transcript,
it can remain in the prompt for every later turn.

A 100,000-token build log produced near the start of a 20-turn investigation
can occupy the working context for the rest of the task. A larger context
window raises the limit. It does not make those bytes useful on every turn.

The common fixes all lose something:

- Keep the full output, and the transcript fills with old evidence.
- Truncate it, and the useful line may be in the discarded middle.
- Summarize it, and the original evidence becomes hard to verify.
- Compact later, and the session loses detail after already carrying the cost.

straitjacket separates evidence from active context. Complete output goes to a
local artifact store. The transcript keeps a bounded view of what happened and
how to retrieve the rest.

## What changes for a developer

| Need | Command | Result |
|---|---|---|
| Run a noisy command | `ctx run -- <command>` | Bounded digest; full stdout and stderr retained |
| Read a bounded region | `ctx get <handle> --lines A:B` | Bounded retrieval from stored evidence |
| Find a fact in stored output | `ctx search <handle> <pattern>` | Matches without replaying the artifact |
| Compare two runs | `ctx diff run:8d8335db6848 run:5a67c9de0123` | Outcome and evidence changes |
| Navigate a repository | `ctx map`, `ctx def`, `ctx refs`, `ctx callers` | Structural views instead of broad file dumps |

The basic workflow stays familiar. Run the command. Inspect the failure. Read
more only when the next decision needs it.

## One run

```bash
ctx run -- pytest -q
```

A large result becomes a digest like this:

```text
[ctx run:8d8335db6848 profile=pytest/v2]
exit: 1
stdout: 4,102 lines · 402.1 KiB · est 98,000 tokens
failures:
  tests/test_auth.py::test_token_expiry  tests/test_auth.py:42
coverage:
  identities: 1/1
  omitted: 4,098 lines
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```

The fields vary by output profile. The contract does not: state the outcome,
show the evidence that is likely to affect the next decision, declare what was
omitted, and provide a route back.

```bash
# Read the cited region.
ctx get run:8d8335db6848#stdout --lines 1280:1300

# Search the complete stored output.
ctx search run:8d8335db6848#stdout "MissingTenantError"

# Compare the failed run with the run after a fix.
ctx diff run:8d8335db6848 run:5a67c9de0123
```

Retrieval is bounded too. Asking for a large region returns a smaller view with
continuation addresses rather than creating a second flood.

Handles address immutable stored bytes while an artifact is retained.
Model-visible retrieval remains subject to the current redaction policy; when
redaction changes an exact-byte request, the response says so.

## How it works

1. `ctx run` captures stdout and stderr directly. Claude Code hooks can rewrite
   recognized floods before execution. The Codex path implements and
   contract-tests the same gates, but still lacks a live CLI receipt.
   Antigravity can deny a recognized flood and name the bounded command, but
   cannot replace output.
2. A typed profile extracts identities, locations, counts, rare events, and
   other decision-relevant facts.
3. Raw bytes are retained in an immutable local artifact.
4. A deterministic renderer emits a bounded digest with coverage and retrieval
   addresses.
5. `ctx get`, `ctx search`, and `ctx diff` page evidence back in on demand.

Profiles are specific to the shape of the output:

| Output | Digest keeps |
|---|---|
| Tests | Failed identities, locations, and outcome census |
| Diagnostics | Severity, code, file, and line |
| Logs | Rare templates, repeated families, head, and tail |
| JSON / JSONL | Shape, counts, and exceptional records |
| Search | Matches, files, and coverage |
| Generic text | Bounded windows and addresses for the rest |

Short output can pass through unchanged. Containment is for results that can
grow beyond the value they add to the current turn.

## Addresses, not summaries

Frozen artifacts have immutable handles:

```text
run:8d8335db6848#stdout
snapshot:fe21c91ad4e8
blob:7bd91f2a4c3d
```

Repository files are mutable, so line numbers alone are unsafe. A repository
address can include a content anchor:

```bash
ctx get repo:src/auth.py --lines 40:52@07407f1c
```

On retrieval, straitjacket verifies the original position, relocates the same
content if it moved, or refuses if the content no longer exists. It does not
quietly return whatever now occupies the old lines.

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

`ctx setup` preserves unrelated user-owned settings. It merges JSON settings
where safe, refreshes ctx-managed files and marker-delimited blocks, and prints
a reviewed snippet instead of rewriting user-owned Codex TOML.

| Host | Before execution | After execution |
|---|---|---|
| Claude Code | Transparent command rewrite | Yes |
| Codex | Implemented and contract-tested | Implemented and contract-tested; live CLI receipt pending |
| Antigravity | Deny known floods and return a bounded replacement command | No |

Antigravity's published hook contract cannot mutate pre-tool arguments or
replace post-tool output. straitjacket states that limit instead of claiming
transparent containment where the host does not permit it. See
[Host capabilities](docs/HOST-CAPABILITIES.md).

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

1. [How it works](docs/HOW-IT-WORKS.md)
2. [Getting started](docs/GETTING-STARTED.md)
3. [Why straitjacket](docs/WHY-STRAITJACKET.md)
4. [CLI guide](docs/CLI.md)
5. [Documentation map](docs/README.md)

Reference: [configuration](docs/CONFIGURATION.md) ·
[troubleshooting](docs/TROUBLESHOOTING.md) ·
[architecture](docs/ARCHITECTURE.md) ·
[specifications](spec/) · [changelog](CHANGELOG.md)

## Development

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e '.[dev]'
pytest
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing a mechanism. New
mechanisms need deterministic output, explicit degradation, and a named
evaluation gate.
