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
| Computed control flow | `ctx py <script>` | Branch, loop, aggregate; one bounded final digest |
| Long-running work | `ctx run --bg-after N -- …` | Returns a job handle instead of idling |
| Inspect a job | `ctx job <id>` | Bounded live tail and lifecycle control |
| Exact evidence | `ctx get <handle>` | Address in, exact bytes or bounded zoom out |
| Search stored evidence | `ctx search …` | Search artifacts without re-execution |
| Compose typed facts | `ctx q '<pipeline>'` | Total, bounded repository/evidence query algebra |
| Answer a question | `ctx ask "…" --intent <i>` | Typed intent preset (locate/impact/diagnose) → one evidence view |
| Compare two runs | `ctx diff run:A run:B` | Behavioral delta instead of two complete outputs |
| Inspect or compare binary evidence | `ctx image digest …` / `ctx image diff …` | Typed structure and identity without inlining pixels or PDF bytes |
| Session scorecard | `ctx stats --session` | Wire residency, rounds, behavior and interventions |
| Cumulative savings | `ctx gain` | Containment savings by command family/verb |
| Replay histories | `ctx replay …` | Read-only counterfactual analysis over recorded sessions |

### Understand code without reading it into context

| Need | Command | Why |
|---|---|---|
| A map of the repo | `ctx map --budget N` | Ranked, token-budgeted file/symbol map instead of a directory dump |
| Where a symbol lives | `ctx def <symbol>` | Definition site as a snapshot + span |
| Who uses a symbol | `ctx refs <symbol>` | Reference sites, bounded |
| Who calls / what it calls | `ctx callers <symbol>` / `ctx callees <symbol>` | Call graph, one query instead of a recursive grep |
| Blast radius of a change | `ctx impact <symbol> --depth N` | Transitive callers (`--depth ≤ 6`) |
| What implements a type | `ctx impls <Type> --depth N` | Subtypes with coordinates, plus what the type itself extends |
| Why the import fails | `ctx cycles` / `ctx cycles --calls` | Circular imports between files, or mutual recursion between functions |
| Lint/syntax digest | `ctx diag <path>` | Deterministic diagnostics without running a full linter into context |
| A compiled investigation | `ctx plan …` / `ctx plan run …` | Validate, price, and run a bounded DAG of evidence ops locally; get one digest |

### Manage the store and the session

| Need | Command | Why |
|---|---|---|
| Set up a workspace | `ctx init` | Write `ctx.toml` + `.ctxignore` |
| Verify the install | `ctx doctor` | Validate hooks, manifests, store, and classifier |
| Harness / unharness a host | `ctx wrap …` | Install or inject host integration (see [Getting started](GETTING-STARTED.md)) |
| Freeze a cache epoch | `ctx checkpoint` | Mark a task boundary for lossless rescue |
| Protect / reclaim storage | `ctx pin` / `ctx gc` | Retention leases and mark-and-sweep |
| Track deferred decisions | `ctx debt …` | Declared-omission ledger (`add`/`list`/`resolve`) |
| Inspect steering policy | `ctx policy show` | Print the compiled, committed policy |

Full flags for every verb live in the skill reference
([`verbs.md`](../plugins/antigravity/skills/ctx-harness/references/verbs.md)).

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

`--` ends straitjacket’s options. Everything after it is the child command.

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
capture, and intermediate storage remain local. It is preferable to `ctx py` when no
computed control flow is needed.

## Execute computed control flow: `ctx py`

Use eval when a script must branch, loop, or aggregate structured intermediate results.

```bash
ctx py investigation.py
```

The script itself is stored as an addressable artifact. Intermediate command output does
not enter the transcript; failures remain deterministic and retrievable.

`ctx py` provides bounded capture, not OS isolation. Treat it as having the same
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

### Line addresses into files you are editing

A `run:` or `blob:` handle names frozen bytes, so its line numbers cannot go
stale. A `repo:` handle names a live worktree file, where a line number is a
position rather than an identity — insert two imports above it and the same
address returns different code. Append a **content anchor** to say what was
there:

```bash
ctx get repo:app/auth.py --lines 40:52@07407f1c
ctx get repo:app/auth.py --lines 40:52 --hashlines   # L40:a3| … per-line tags
```

The anchor verifies silently when the content has not moved, **follows it** when
it has (declaring `anchor: @07407f1c moved L40:52 → L42:54` and echoing the
corrected address), and **refuses with exit 2** when the content is gone rather
than returning whatever now sits at those coordinates. `ctx def` emits an
anchored `live:` address for exactly this reason. Full mechanism:
[ANCHORS.md](ANCHORS.md).

## Search captured artifacts: `ctx search`

Use search when the evidence already exists in the store:

```bash
ctx search 'MissingTenantError'
ctx search 'authorization failed' --run run:<id>
```

Searching an artifact is cheaper and more trustworthy than rerunning a command merely
to recover text the harness already captured.

## Walk the call graph: `ctx callers` / `callees` / `impact` / `impls`

```bash
ctx callers Store.put_blob            # who calls it, with the call-site line
ctx callees digest_output             # what it calls, in-repo only
ctx impact Store.put_blob --depth 4   # transitive callers (blast radius)
ctx impls Profile                     # what implements or extends this type
ctx cycles                            # circular imports between files
ctx cycles --calls                    # mutual recursion between functions
```

`ctx cycles` answers an operational question, not an aesthetic one: a circular
import is *why the module fails to load*, and a recursion cycle is *why the
stack blew*. Components are found with Tarjan's algorithm (networkx when it is
importable, an iterative stdlib implementation otherwise — identical output,
verified by test) and printed largest first.

Edges come from the languages the skeleton tier parses — Python, plus
JavaScript, TypeScript, Go and Rust with the `[code]` extra. The engines in
force are printed in the header of every answer.

### Scoped by default, and the rest is one flag away

A call to `render` could name any `render` in the repo. Rather than guess, each
call site is resolved in tiers and the first non-empty one wins:

| tier | the call binds to | stated as |
|---|---|---|
| `local` | a definition in the calling file | fact |
| `import` | a definition in a file the caller **directly** imports | fact |
| `repo` | any definition with that name, anywhere | candidate |

Only the first two are reported by default. Repo-wide matches are held back
with their count and the flag that resolves them, so the default answer is one
you can act on and the wider net is never silently mixed in:

```
callers: 1
    detect_profile  src/ctx/digest/__init__.py:61
  omitted: 35 UNSCOPED callers (name matched repo-wide; the caller's file
    neither defines nor imports the target)
    resolve: ctx callers LogTemplateProfile.detect --unscoped
```

`--unscoped` widens `callers`, `callees` and `impact`; the widened rows stay
marked `[unscoped]` so a candidate never reads as a fact. When several
definitions answer to one name, every one of them is listed before the results
— an ambiguous question gets an ambiguous answer, out loud.

## Answer a question: `ctx ask`

```bash
ctx ask "Where is AuthContext defined and used" --intent locate
ctx ask "What could break if CacheKey.build changes" --intent impact --symbol CacheKey.build
ctx ask "Why are the authentication tests failing" --intent diagnose
ctx ask "Where is TokenBucket defined" --intent locate --plan   # show the compiled plan, don't run
```

`ctx ask` compiles a repository question into a typed intent preset — a frozen
`ctx.plan/v1` — and runs it on the plan executor, answering with the investigate
digest. Seven intents ship — five that observe and two that execute:
`locate` (where is X defined and used), `impact` (what could break if X
changes), `diagnose` (what explains the captured failures — reads captured
facts, never reruns tests), `trace` (how control/data flows through X),
`compare` (what differs between two runs), `verify` (what proves a change is
correct — execute-class, runs the test command), and `review` (what changed,
what is risky, what is under-verified — execute-class). There is no
natural-language parser: `--intent` is required
(a missing one is a teaching error that suggests, never guesses), and the subject
is `--symbol` or the question's sole identifier-shaped token, always disclosed.
See [docs/ASK.md](ASK.md).

## Compose evidence: `ctx q`

```bash
ctx q 'fails last | in-changed'
ctx q 'refs TokenBucket | group file | top 5'
ctx q 'fails last | shared-cause | top 10'
ctx q 'corpus --ext py --changed | outline'
ctx q 'records run:<id>#stdout --jsonl | group level | count'
ctx q 'search TODO --glob "src/*.py" | histogram file'
```

`ctx q` operates over typed record streams such as failures, symbols, files, and sites.
`corpus` selects a bounded eligible file set with a coverage receipt (`--changed` binds
to worktree generations, never mtime); `records` opens a stored JSON/JSONL artifact as a
record stream; `distinct` and `histogram` summarize any field.
The algebra is deliberately total: bounded stages, no loops, no recursion. This makes
costs statically boundable and every stage’s result addressable.

Use `ctx py` when the control flow is genuinely computational. Use `ctx q` when the
intent is a bounded composition of repository and evidence facts.

## Compare runs: `ctx diff`

```bash
ctx diff run:<before> run:<after>
```

The comparison should answer the verification question directly: what failures,
templates, exits, signals, or stream sizes changed? New evidence receives coordinates.

## Inspect binary evidence: `ctx image`

```bash
ctx image digest screenshots/home.png reports/month-end.pdf
ctx image diff screenshots/before.png screenshots/after.png
```

`digest` prints a bounded structural view: magic-byte format, byte size, exact
SHA-256 identity, common image dimensions and colour mode, or labelled PDF
structure heuristics. It never prints the image pixels or PDF body.

`diff` compares two decodable images using a deterministic 64-bit dHash and
also reports whether their bytes are identical. Install the optional decoder
with `python -m pip install -e '.[image]'`. A dHash is a coarse render-change
signal, not a semantic or aesthetic judgment; inspect the actual raster when
visual correctness matters.

Paths are confined to the workspace and respect `.ctxignore`. Binary output
captured by `ctx run` selects `binary/v1` automatically and keeps the complete
artifact behind its run handle.

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

## Score the loop: regret, follow-up, shadow

```bash
ctx replay --regret <t.jsonl>            # per-profile frontier gap (docs/THEORY.md)
ctx replay --outcomes <t.jsonl>          # per-operator follow-up counts (association, not causation)
ctx replay --outcomes --append-ledger …  # explicit: feed the workspace follow-up ledger
ctx policy compile --plan-value          # aggregate ledger → committed [plan_value] COUNTS
ctx plan price --value <plan.json>       # price card + shadow follow-up ranking (report only)
ctx plan run --advise <plan.json>     # digest + shadow report + shadow ledger line
```

Counts, not rates, in the committed table; Wilson lower bounds derive at
read time. The shadow ranking never reorders or suppresses anything —
promotion to a conservative tie-break waits on the paired referee, and
hard constraints always dominate. Runtime never writes the policy file.

## Failure semantics

straitjacket distinguishes safety from optional intelligence:

- safety gates fail closed when allowing an operation could violate the containment
  invariant;
- optional extractors, indexes, and accelerators fail open to a labeled lower-precision
  mode;
- degraded precision is disclosed in the digest;
- omission is declared and addressed, never silent.

## Exit codes

`ctx` is invoked from hooks, wrappers, CI steps, and — most often — by an agent
deciding what to do next, so its exit status is part of the interface. Six codes are
used, and they answer three different questions.

| Code | Meaning | What the caller should do |
|---|---|---|
| `0` | Success | Continue |
| `1` | **ctx** failed | Not your invocation's fault: an internal error, an unreadable store, an engine that would not start. Retry or run `ctx doctor` |
| `2` | ctx **rejected the invocation** | Fix the arguments. Unknown command or flag, a malformed selector (`--lines nope`), an ungrammatical reference (`zzz:xyz`), a missing required argument, a workspace that will not resolve, or a handle that no longer resolves because `ctx gc` or the retention window collected it |
| `3` | **The thing you asked about** failed | ctx worked; the child command, script, sequence step, or job exited nonzero. The digest is the evidence — read it, do not re-run the command to see the output |
| `124` | Timed out | The child exceeded `--timeout` (or `ctx job --wait` gave up). Matches `timeout(1)` |
| `127` | Not found | The program ctx was asked to launch or wrap is not on `PATH`. Matches the shell's convention |

The important line is between `1`, `2`, and `3`. A script that treats every nonzero
status as "the command failed" will re-run work that already produced its evidence
(`3`), and retry invocations that will never succeed as typed (`2`).

Human-readable errors go to **stderr**; digests, reports, and status output go to
**stdout**. An error message names the verb that produced it (`ctx get: …`), so a
failure in a pipeline is attributable without a traceback.

### When the message is not enough

```bash
CTX_DEBUG=1 ctx <command> …
```

`CTX_DEBUG` prints the real traceback for an unhandled error, on the CLI and on the
MCP server alike. Without it, an exception that escapes a handler is summarized as
`ctx <command>: <ExceptionType>: <message>` — enough to attribute the failure, not
enough to fix it.

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
