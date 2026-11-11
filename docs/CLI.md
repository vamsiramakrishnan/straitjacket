# CLI guide

The CLI is organized around four operations:

1. **capture** an operation before it can flood;
2. **query** stored evidence without replaying raw output;
3. **resolve** an address to selected stored coordinates or a bounded sub-digest;
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
| Stored evidence | `ctx get <handle>` | Address in, redaction-aware region or bounded zoom out |
| Search stored evidence | `ctx search …` | Search artifacts without re-execution |
| Compose typed facts | `ctx q '<pipeline>'` | Total, bounded repository/evidence query algebra |
| Answer a question | `ctx ask "…" --intent <i>` | Typed intent preset (locate/impact/diagnose) → one evidence view |
| Compare two runs | `ctx diff run:A run:B` | Behavioral delta instead of two complete outputs |
| Inspect or compare binary evidence | `ctx image digest …` / `ctx image diff …` | Typed structure and identity without inlining pixels or PDF bytes |
| A large paste, file or clipboard | `ctx intake [PATH\|-]` | Stores the text once; returns a digest, a chunk map and a handle |
| An image, screen recording or video | `ctx view <target>` | Cheap overview first, then crops or frames from the original |
| Which tool calls ctx governs | `ctx coverage` | The routing table per host, and what the hooks observed |
| Session scorecard | `ctx stats --session` | Wire residency, rounds, behavior and interventions |
| Cumulative savings | `ctx gain` | Containment savings by command family/verb |
| Replay histories | `ctx replay …` | Read-only counterfactual analysis over recorded sessions |

### Understand code without reading it into context

| Need | Command | Why |
|---|---|---|
| A map of the repo | `ctx map --budget N` | Ranked, token-budgeted file/symbol map instead of a directory dump |
| Exact answers from def/refs | `ctx index` | Build a compiler-grade SCIP index once; def/refs stop approximating |
| Where a symbol lives | `ctx def <symbol>` | Definition site as a snapshot + span |
| Who uses a symbol | `ctx refs <symbol>` | Reference sites, bounded |
| Who calls / what it calls | `ctx callers <symbol>` / `ctx callees <symbol>` | Call graph, one query instead of a recursive grep |
| Blast radius of a change | `ctx impact <symbol> --depth N` | Transitive callers (`--depth ≤ 6`) |
| What implements a type | `ctx impls <Type> --depth N` | Subtypes with coordinates, plus what the type itself extends |
| Why the import fails | `ctx cycles` / `ctx cycles --calls` | Circular imports between files, or mutual recursion between functions |
| Lint/syntax digest | `ctx diag <path>` | Deterministic diagnostics without running a full linter into context |
| A compiled investigation | `ctx plan …` / `ctx plan run …` | Validate, price, and run a bounded DAG of evidence ops locally; get one digest |
| Explicit model analysis | `ctx semantic prepare/run/resume/show …` | Frozen evidence, bounded worker calls, validated citations, and durable partial reports |
| Investigate and verify a repair | `ctx task prepare/run/resume/show/apply …` | Shared task budget, retained worktree, adaptive evidence gathering, and independent checks |

See [semantic analysis](SEMANTIC.md) for the request/worker protocol and the
distinction between admission estimates and hard process bounds.

### Edit and verify observed source

| Need | Command | Why |
|---|---|---|
| Preview or apply one observed replacement | `ctx edit replace …` | Snapshot or anchored span, addressed plan and receipt |
| Review and apply a sealed plan | `ctx edit preview …` / `ctx edit apply …` | Check current bytes before writing |
| Check an applied edit | `ctx edit verify … -- <command>` | Captured check bound to edited files and declared witnesses |
| Request a verified continuation | `ctx edit handoff …` | Attempt-bound proof and complete bounded checklist |
| Expand a demonstrated rewrite | `ctx edit expand …` | Explicit structural rule, scoped preview, ordinary plan |
| Compare measured strategies | `ctx edit advise …` | Paired quality and total-cost gates |

See the [edit loop](EDIT-LOOP.md) for command examples, recovery, and proof limits.

### Manage the store and the session

| Need | Command | Why |
|---|---|---|
| Set up a workspace | `ctx init` | Write `ctx.toml` + `.ctxignore` |
| Verify the install | `ctx doctor` | Validate hooks, manifests, store, and classifier |
| Harness / unharness a host | `ctx wrap …` | Install or inject host integration (see [Getting started](GETTING-STARTED.md)) |
| Shrink what a wrapped Claude Code session loads | `ctx wrap claude --surface …` | Launch-time capability plan with a receipt |
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

```bash
ctx setup --prune      # configure the installed agents, then defer what this repo does not use
ctx prune              # preview the same decision on an already-harnessed repo
ctx prune --apply --host claude --host codex
```

`ctx prune` is the capability-surface audit run as a setup step with a decision
rule: ctx's own kernel and every capability that is read-only or observed in use
stays visible; a skill, agent or MCP server that is unused, remote-write or
destructive is deferred, and each host's minimal launch config is compiled from
that selection (`.ctx-surface/`). Nothing is deleted. The receipt
(`.ctx-surface/prune-receipt.json`) records the per-turn tokens before and after
and what the repository looked like when the decision was made. `--keep <id>`
pins a capability the rule would defer; `--probe-mcp` spawns each MCP server to
measure its real schema tokens instead of estimating them.

On a terminal, `ctx prune` and `ctx setup --prune` ask rather than decide. One
selector per group (MCP servers, skills, agents) lists each capability with its
tokens per turn, authority, observed use and the rule's recommendation marked
`[defer]` or `[keep]`:

```text
MCP servers: 2 · 1,240 tok/turn
   1. [keep ] mcp.github                                   980 tok/turn  remote-write  used 12x
   2. [defer] mcp.deploy-prod                              260 tok/turn  destructive   never used
  defer which? [Enter = as marked]
```

Enter accepts the marks; `all` / `none`; `1,3-5` picks exactly those; `+2` /
`-3` adjusts the marks; `?2` explains one item. Kernel capabilities (ctx's own
tools, policy, repository instructions) are reported as kept and never asked
about. Then the hosts to compile for, then a confirmation before anything is
written. The receipt records, per capability, whether the rule or the user
decided. `--yes` takes the rule's answer at every prompt without asking, which is
also what a pipe or `--json` gets; `--interactive` forces the questions.

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

For an embedding tool, `ctx run --json -- <command>` prints one
`ctx.command-result/v1` object with the command's exit code or signal, timeout
state and a full run reference. Complete small text streams remain separate;
large output uses the existing bounded digest. The CLI still exits `3` on a
failed command and `124` on timeout. Read the JSON command status instead of
inferring it from ctx's exit code. This foreground mode rejects `--bg`,
`--bg-after` and `--passthrough`. See [Tool SDK](TOOL-SDK.md) for the contract.

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

## Retrieve bounded evidence: `ctx get`

```bash
ctx get run:8d8335db6848#stdout --lines 120:180
ctx get blob:7bd91f2a4c3d
ctx get 0f44195122
```

Small requests select exact stored coordinates. Model-visible bytes remain
subject to the current redaction policy; an exact-byte request declares when
redaction changed it. A region too large for the retrieval budget returns a
bounded zoom digest with further spans. A span id printed in a digest is honoured
only on the artifact the reference names: `ctx get run:A#stdout --span <id from run B>`
is refused with "span … does not belong to …; use the reference the digest printed with
it". Retrieval cannot recursively re-flood the transcript.

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

`--snapcompact` renders the selected slice as a deterministic monospace
bitmap PNG (`ctx.snapcompact`) and returns a `blob:` ref instead of text —
an opt-in cost/format tradeoff (requires the `image` extra: `pip install
'ctx-harness[image]'`). It never changes default behavior, and this repo
does not claim a live vision model actually reads the image back correctly
at any cost saving — see `ctx.snapcompact`'s module docstring:

```bash
ctx get run:7bd91f2a4c3d#stdout --lines 1:120 --snapcompact
ctx get blob:<hash from the header> --bytes 1:<size>   # fetch the PNG bytes
```

The anchor verifies silently when the content has not moved, **follows it** when
it has (declaring `anchor: @07407f1c moved L40:52 → L42:54` and echoing the
corrected address), and **refuses with exit 2** when the content is gone rather
than returning whatever now sits at those coordinates. `ctx def` emits an
anchored `live:` address for exactly this reason. Full mechanism:
[ANCHORS.md](ANCHORS.md).

## The collaboration record: `ctx task`

```bash
ctx orchestrate "add a caching layer"                     # prints task: <id> at the end
ctx task ls                                               # tasks with a ledger, newest first
ctx task show task-18d1583556af4e76                       # claims · handbacks · steward · inbox
ctx task send task-18d… implement checkpoint:d914ee702801 --note "the plan"
ctx task inbox task-18d… implement
ctx orchestrate --resume task-18d1583556af4e76            # finished nodes restored, the rest run
```

Every `ctx orchestrate` run writes a task ledger; `show` renders it as
addresses and numbers. `send` hands a node an **address** — never content —
which the node sees in its prompt and resolves with `ctx get`. Full
mechanism: [TASK-LEDGER.md](TASK-LEDGER.md).

## Tell another harness: `ctx relay`

```bash
ctx relay watch claude job --action report          # tell me when a job lands
ctx run --bg -- pytest -q                           # ... and stop polling
ctx relay signal codex:0f1e2d checkpoint:d914ee702801 --interrupt \
    --note "the migration schema changed under you"
ctx relay status                                    # queued, watching, pending
```

The task ledger lets harnesses share a record; the relay lets one **tell**
another. A signal carries an address and a bounded note, never content — the
same closed grammar `ctx task send` enforces.

Delivery is at the receiving harness's next hook boundary: `post-tool-use` and
session start for `report`/`advise`, `pre-tool-use` for `interrupt`. There is no
push, and on a hooked host an interrupt stops the next **tool call**, not a
token stream. An [ACP worker](ACP.md) is the exception — ctx owns that
subprocess, so a queued interrupt cancels its turn mid-flight and a queued
report is prepended to its prompt. Full mechanism, per-host delivery table,
bounds and failure direction: [RELAY.md](RELAY.md).

## Search captured artifacts: `ctx search`

Use search when the evidence already exists in the store:

```bash
ctx search run:8d8335db6848#stdout 'MissingTenantError'
ctx search run:8d8335db6848 'authorization failed'
```

Searching an artifact is cheaper and more trustworthy than rerunning a command merely
to recover text the harness already captured.

### Search the repository: filters, symbols, history

Repository searches take the query language (docs/CODE-SEARCH.md): filters ride
in the pattern list, the text index narrows the corpus to trigram candidates
(synced by fingerprint before every query, verified against the live bytes),
and history is a search target like any other.

```bash
ctx search repo: TokenBucket file:src/ '!file:tests' lang:python
ctx search repo: 'rate limit' case:no
ctx search repo: sym:resolve_refs                  # definition sites from the symbol table
ctx search repo: type:commit "prefix tax"          # commits whose message says so
ctx search repo: type:diff ENABLE_TOOL_SEARCH file:src/   # commits whose change carries it
```

The coverage line names the engine (`index trigram · 45 ms sync`, ripgrep, or
the Python scan) and, for the index, how many of the corpus's files were read
to verify. `CTX_SEARCH_ENGINE=rg|python` bypasses the index for one call;
`CTX_SEARCH_INDEX=off` disables it.

## Answer exactly, not approximately: `ctx index`

```bash
ctx index --list                 # which indexers are installed, what this repo needs
ctx index                        # refresh the text index, then index the dominant language
ctx index --language rust        # or name one
ctx index --text                 # only the text index (trigrams, symbols, fingerprints)
ctx index --status               # what is indexed, and how current it is
```

Two indexes live here. The **text index** (docs/CODE-SEARCH.md) needs no
toolchain, builds itself on first search below a size guard (4,000 files /
48 MB) and is re-synced by a stat sweep before every query, so `--text` is a
warm-up, not a requirement. The **SCIP index** is the exact tier below.

`ctx def` and `ctx refs` run an engine ladder, and the top rung is a SCIP index
— the answer the language's own compiler front end gives. ctx has read those
indexes for a long time and never made one, so unless a repository indexed
itself the rung was unreachable and answers came from the ladder's floor: a
word-boundary regex over source files.

That floor is worse than it looks. Measured against `rust-analyzer scip` on
`tokio-rs/bytes`, it reported **3,637 reference sites where the compiler says
806** — and the error lands exactly where names are short and common, which is
where you ask. `ctx refs buf` returned 754 sites, 55 of them real. A regex
cannot tell a reference from the same letters in a comment or a string literal;
the information is not in the text.

`ctx index` shells out to the real tooling rather than approximating it:

| language | indexer | install |
|---|---|---|
| rust | `rust-analyzer scip` | `rustup component add rust-analyzer` |
| go | `scip-go` | `go install github.com/sourcegraph/scip-go/cmd/scip-go@latest` |
| typescript / javascript | `scip-typescript` | `npm i -g @sourcegraph/scip-typescript` |
| python | `scip-python` | `npm i -g @sourcegraph/scip-python` |
| java | `scip-java` | see `sourcegraph/scip-java` |

The index is written to the store's audit area, **never into your worktree**.
Indexing stays an explicit command because it costs seconds to minutes and
needs the project's toolchain — it must never happen inside a retrieval verb on
a hook's latency budget.

### An index goes stale, and ctx will not pretend otherwise

A SCIP index is a snapshot of one moment; nothing keeps it in step with your
edits. Trusting a stale one is worse than having none, because the exact tier
answers in a confident voice: a symbol you added since indexing would come back
as `sites: 0`, and a call site added to a file that changed would simply be
missing from an answer still labelled `scip (exact)`.

So before using an index, ctx checks that no source file is newer than it (and,
for an index ctx built, that the file count still matches — a deletion moves no
surviving file's timestamp). If the tree has moved on, the index is skipped
exactly as if it were absent, the ladder falls through, and the header says so:

```
[ctx refs helper · engine ast (textual) · stale index skipped, re-run ctx index]
```

That check walks the source files, which measured ~220 ms on a 4,700-file
worktree. It is charged on every `refs`/`def` call in an indexed repository,
and it buys the difference between an exact answer and a plausible one — when
the verdict is "stale", the textual scan that runs instead costs far more.

An index `ctx index` built also records the content hash of every source file
it described, and that makes a stale index *partially* usable instead of
useless: files whose hash still matches are answered exactly, the changed or
new ones by the textual rung restricted to them, and the header says how many:

```
[ctx refs helper · engine scip (exact) · 1 changed file via ast (textual)]
```

`ctx index --status` lists the files the index no longer describes; `ctx impls`
has an exact rung from the index's own implementation edges.

Nothing about this is required. With no index the ladder keeps its lower rungs
and every answer still discloses which engine produced it. `ctx doctor` carries
an `exact index` row saying whether one exists, whether it is still current,
and what to run if not, so the difference is visible rather than inferred.

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

## Ask the language server: `ctx lsp`

The skeleton (`ctx stats repo:<file>`) says *where* symbols are, from a parser.
A language server says what a name at a position *means*: its definition across
files, every reference to it, its type and signature. `ctx lsp` is a bounded
JSON-RPC client over stdio for any server on `PATH`, one process per call.

```bash
ctx lsp def   src/app.py:42:13            # definition of the name at line 42, col 13 (1-based)
ctx lsp refs  repo:src/app.py:Router.dispatch   # references; position taken from the skeleton
ctx lsp hover src/app.py:42:13 --timeout 60     # type and signature
```

Output is the same coordinate shape as `ctx refs` (`repo:<path>:L<n>: <text>`),
workspace-confined, with the engine disclosed in the header (`engine lsp
(pyright-langserver)`). Servers are looked up per skeleton language: pyright,
pylsp or jedi-language-server for Python; typescript-language-server; gopls;
rust-analyzer; clangd; jdtls; kotlin-language-server; lua-language-server;
intelephense or phpactor; ruby-lsp or solargraph; metals; bash-language-server;
sourcekit-lsp; csharp-ls or omnisharp. `CTX_LSP_SERVERS` (JSON of the same
shape, `{"python": [["pylsp"]]}`) replaces the table. `ctx doctor` lists which
server each language resolved to. No server for a language exits `2`; a server
that fails or times out exits `3`; `ctx refs` uses the tier as a rung above jedi
and the textual fallback when a server is present, and says so.

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
ctx q 'records run:8d8335db6848#stdout --jsonl | group level | count'
ctx q 'search TODO --glob "src/*.py" | histogram file'
```

```bash
ctx q 'commits "rate limit" | touched | top 5 | outline'   # commits → files they touched → outlines
ctx q 'search TokenBucket file:src | history'              # each site with the commit that last changed its file
ctx q 'search retry lang:python | history --line'          # per line (git blame)
```

`ctx q` operates over typed record streams such as failures, symbols, files, and sites.
The `search` stage takes the query language's filters and narrows through the text
index; `commits` turns git history into records, `touched` into the files they name,
and `history` annotates sites or files with their last commit (docs/CODE-SEARCH.md).
`corpus` selects a bounded eligible file set with a coverage receipt (`--changed` binds
to worktree generations, never mtime); `records` opens a stored JSON/JSONL artifact as a
record stream; `distinct` and `histogram` summarize any field.
The algebra is deliberately total: bounded stages, no loops, no recursion. This makes
costs statically boundable and every stage’s result addressable.

Use `ctx py` when the control flow is genuinely computational. Use `ctx q` when the
intent is a bounded composition of repository and evidence facts.

## Where to look first: `ctx pack`

```bash
ctx pack "Add partial_structure to BaseConverter; return a PartialResult …"
ctx pack @task.md --files 10 --budget 3000
ctx pack - --json < task.md
```

A ranked, budgeted context pack for a task: the files the task's terms, the
symbol table, the paths and the history point at, each with its outline and the
reason it is there (`why: terms … · defines … · commit …`). Terms are weighted
by rarity from the text index and verified as whole-word counts in the file's
bytes; tests and prose are demoted unless the task is about them. Deterministic
for a tree state; `evals/agentbench/pack_recall.py` measures it against the
files DeepSWE reference solutions actually change (docs/CODE-SEARCH.md).

The first three ranked files can also carry short source excerpts with snapshot
addresses. Matched entry points include bounded, file-scoped caller/callee rows
from the static graph. These are AST-derived relationships, not LSP guarantees;
ambiguous targets retain their candidate counts. Small budgets omit excerpts.
The index keeps separate branch catalogs and resyncs dirty files on every query.
See [code search](CODE-SEARCH.md) for cache lifetime and the edit/verify workflow.

## ctx as the host: `ctx agent`

```bash
pip install 'ctx-harness[agent]'
ctx agent -p "the task" --model haiku --max-turns 60 --output-format json
ctx agent -p @task.md --pack --verbose
```

Runs a print-mode session on the Claude Agent SDK with ctx as the host: a lean
built-in surface (Bash, Read, Edit, Write, MultiEdit), the retrieval verbs as
in-process tools (`search`, `outline`, `get`, `refs`, `pack`), the wrapper's
hooks, and with `--pack` a context pack in the first turn (off by default: on
DeepSWE with haiku it cost held-out tests, see the receipt). The SDK drives the same `claude`
binary, so billing, caching and the login are the host's; the result JSON is
the host's shape plus `pack` and `runtime`. Exit 2 without the extra.

## Compare runs: `ctx diff`

```bash
ctx diff run:8d8335db6848 run:5a67c9de0123
```

The comparison should answer the verification question directly: what failures,
templates, exits, signals, or stream sizes changed? New evidence receives coordinates.

## Evidence that travels: `ctx capsule`

A `run:` handle resolves in exactly one store. Cited in a pull request, it is
a claim a reviewer cannot check; cited from a container that has since been
reclaimed, it is a claim nobody can check. A capsule is one file holding the
manifests a set of handles names and the blobs those manifests reference —
no task text, no prompts, no transcript, the same export-safe rule the task
ledger follows.

```bash
ctx capsule export evidence.ctxcap --handle run:8d8335db6848#stdout
ctx capsule export evidence.ctxcap --task task-19f3c2a1b0e   # everything that task cited
ctx capsule verify evidence.ctxcap
ctx capsule import evidence.ctxcap    # then every cited handle resolves here
```

Export closes transitively over what the manifests reference, following both
full hashes and the abbreviated `run:8d8335db6848` form a checkpoint stores,
and reports any cited handle that resolved to nothing rather than shipping a
capsule with a hole in it. A manifest that carries task text in your own words
— a checkpoint's goal, decisions and hypotheses — is walked for the evidence
it cites and left out, named in the report; `--include-task-text` keeps it.
The file is a POSIX tar with fixed member metadata and no wall-clock field,
so the same evidence always produces the same bytes and two capsules of one
task are identical files.

Every read verifies. `verify` recomputes each member's `sha256` and checks it
against two independent records: the capsule index, and the member's own
address, which for a blob is the hash of its bytes and for a manifest is the
id the store derives from them. It also checks completeness against the closure the exporter
recorded, because integrity is not completeness: a repack that drops a blob
*and* its index row leaves the remaining names agreeing with each other, and
the hole would otherwise surface at the first read of a cited handle.
`import` refuses to write anything at all if one check fails, because a
partial import leaves a handle resolving to bytes nobody vouched for. That check is not habit: `evals/memvid_fidelity.py` found
a single-file memory format returning altered bytes for six of seven payloads
while its own deep verification passed, because those checks covered its index
rather than its content.

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

## Store a paste, file or clipboard: `ctx intake`

A command's output reaches the store because `ctx run` or a hook captured it. Text
that nothing ran, such as a 3 MB log pasted into the chat, a document or the
clipboard, has no such door. `ctx intake` is that door: it stores the bytes once
and prints a bounded digest with a handle.

```bash
ctx intake build.log --label build-log     # a file inside the workspace
ctx intake - < ../downloads/trace.txt      # a file outside it, or any stdin
pbpaste | ctx intake --label paste         # stdin is the default when piped
ctx intake --clip --copy                   # read the clipboard, put the digest back on it
```

```text
[ctx run:690d46e45560 profile=logtemplate/v1]
intake: build-log · build.log · 9,000 lines · 393.5 KiB · est 100,744 tokens if inlined
templates: 2 cover 9,000/9,000 lines
  8,999× L1: <*> step <*> ok compiled module_<*>.c · span 2ca7ca477f
  1× L6001: ERROR: linker failed: undefined reference to `parse_config'
exceptional:
  L6001: ERROR: linker failed: undefined reference to `parse_config'
coverage:
  parsed: 9,000/9,000 lines
  shown: 3 spans · omitted: 8,997 lines
next:
  ctx search run:690d46e45560 'ERROR:' --context 3
  ctx get run:690d46e45560#stdout --lines 6001:6004
```

The digest is the same kind of digest a captured command gets: the profile is chosen
from the shape of the text, and every omission has a `ctx get` or `ctx search`
address. An intake digest describes an intake, never a working directory, command
or exit status. The same bytes under the same label and name produce the same
handle and the same digest bytes; the session's task query never changes them.

| Flag | Meaning |
|---|---|
| `PATH` | A file inside the workspace. It must pass the same path and ignore policy as `ctx image`. A file elsewhere is refused with the way to pipe it. `-` reads stdin, which is also the default when stdin is piped. |
| `--label LABEL` | What this is (`paste`, `log`, `spec`). Part of the identity. Default `file` for a path, `paste` for stdin, `clipboard` for `--clip`. |
| `--name NAME` | Display name, such as the original file name. Part of the identity. Default: the file's base name. |
| `--focus FOCUS` | Terms the digest should favour. Default: none; the digest never borrows the session's task query. |
| `--json` | Print the `ctx.intake/v1` receipt instead of the digest. |
| `--clip` | Read the system clipboard (`pbpaste`, `wl-paste`, `xclip`, `xsel` or PowerShell). Takes no PATH. |
| `--copy` | Put the digest on the clipboard, to paste it in place of the payload. A missing clipboard tool is reported on stderr and the digest still prints. |
| `--chunks HANDLE` | List every chunk of a stored `run:` or `blob:` text handle, one page at a time, instead of storing anything. Takes no PATH or `--clip`. |
| `--from N` | With `--chunks`: the first chunk number to list. |

Labels and names are reduced to letters, digits and `._+-` (48 characters at most)
because they travel in model-visible text.

**What is stored.** Text is stored as UTF-8. Invalid UTF-8 is replaced and the
receipt says `lossy: true`. A UTF-16 payload with a byte-order mark is transcoded
and the receipt names the source encoding. A payload with a NUL in its first 8 KiB,
or in a recognised binary format, is stored as a blob and described by the
`binary/v1` digest (format, size, image dimensions, hash); `ctx view blob:<id>`
then looks at it if it is an image or video. Everything is stored under a retention
lease for `[store] retention_days`, so an explicit `ctx gc --retention-days` does
not collect it earlier. An empty payload is refused. A payload smaller than the
digest that would wrap it is printed as it is, redacted, with the handle on stderr
(`passthrough: true` in the receipt). Stored bytes are not redacted; the digest and
every retrieval are.

**The chunk map.** Text that is large and that no shape profile claims (not JSON,
a log, a trace, a table or a diff) gets `chunkmap/v1`: ordered chunks of about
one retrieval's worth, cut at Markdown headings and blank lines where possible,
each with a line range, a size, a heading or first line and a span id. A chunk
that holds an error-like line is marked `!`, and the strongest such line is named
as the decisive signal.

```text
chunks: 59 (~2.2 KiB each) · shown 16 · error-like 1 (! rows show their signal line)
  #1 L1-L11 · 2.6 KiB · Section 1: beta planning · span 619e1b995d
  #41 ! L441-L453 · 2.7 KiB · ValueError: bad input in section forty · span b68d86de06
  … +43 chunks omitted · list them: ctx intake --chunks run:f3f3063f27b0#stdout --from 2
```

`ctx intake --chunks run:f3f3063f27b0#stdout --from 55` lists more, within the
result budget, and ends with the `--from` that continues. A single line too large for
a chunk, such as minified JSON or base64, is cut into byte-range chunks.

**The 32 MiB limit.** Intake parses at most 32 MiB of a payload. The whole payload
is stored, but only the first 32 MiB is mapped or profiled, cut back to its last
whole line. The digest names the unmapped tail by byte range and prints the
`ctx get --bytes` command that reaches it, so the rest is addressed, not lost:

```text
coverage: 11/11,148 chunks listed · mapped: bytes 1-33,554,426 = L1-L526,024 (parse cap 32.0 MiB) · NOT mapped: bytes 33,554,427-44,688,890 (10.6 MiB, L526,025-L700,000)
next:
  ctx get run:4f4108b8127c#stdout --bytes 33554427:33558970
```

**Pasted traces, tables and diffs.** When a large payload is mostly a stack trace, a
delimited table or a plain unified diff, intake renders a purpose-built view
instead of a chunk map: `trace/v1`, `delimited/v1` or `udiff/v1`. These profiles
apply to intake only, so no command-output digest changes. See
[Pastes and media](PASTES-AND-MEDIA.md#pasted-traces-tables-and-diffs) for what each
view shows and what it guarantees.

Exit status: `0` stored; `1` declined with a reason (empty payload, a file outside
the workspace, an unreadable file, no clipboard tool, a `--from` outside the map,
a binary handle given to `--chunks`); `2` a flag combination that cannot mean
anything. The `ctx.intake/v1` receipt has `kind`, `address`, `label`, `name`,
`bytes`, `lines`, `tokens` (`raw` and `delivered` estimates), `profile`,
`passthrough`, `lossy`, `encoding` and `digest`, plus `copied` with `--copy`.
[Pastes and media](PASTES-AND-MEDIA.md) covers how the agent hosts call intake for
an oversized prompt without being asked.

## Look at an image or video: `ctx view`

Pixels are expensive and a model usually needs one region of them. `ctx view`
gives an overview first, keeps the original addressable, and crops or extracts
from the original when asked.

```bash
ctx view screenshot.png                       # downscaled overview with a labelled grid
ctx view screenshot.png --grid 3x5 --cell B2  # crop one cell from the ORIGINAL pixels
ctx view screenshot.png --region 160,90,320,180
ctx view demo.mp4                             # metadata census and a contact sheet
ctx view demo.mp4 --at 1:30                   # one frame
ctx view demo.mp4 --range 2-9 --every 1       # frames across a stretch
ctx view demo.mp4 --scenes                    # where the picture changes sharply
```

```text
[ctx view shot.png]
view: overview
source: image png 640×360 · 48.3 KiB · sha256 afbd016dee1b…
raw cost: ~299 tok (image-patch, same on either tier)
derived: overview 640×360 image/jpeg · 26.8 KiB · ~299 tok (same on either tier; no saving against the source) · pillow 13.0.0
grid: 3 rows (1-3, top to bottom) × 5 columns (A-E, left to right); cell = column letter + row number, e.g. B2
omitted: metadata (EXIF/ICC) stripped
omitted: grid lines and cell labels are drawn on the overview only
open: .ctx-session-reads/media/overview-d03674112eb5.jpg
stored: blob:d03674112eb5 (this derived file); original shot.png untouched
next: ctx view shot.png --grid 3x5 --cell C2
next: ctx view shot.png --region 160,90,320,180
```

The result is a bounded digest, a workspace-relative file to open (under
`.ctx-session-reads/media/`) and exact `next:` commands. Zoom always reads the
original pixels or frames, never an earlier derivative. Every derived file records
its source hash, the operation, the engine and its version, and the bytes and
estimated tokens before and after.

`TARGET` is a file inside the workspace, or a `blob:<id>` or `run:<id>[#stream]`
address. A file outside the workspace is refused with two ways forward: copy it
in, or capture it with `ctx run -- cat <file>` and view the `run:` address. Audio
and PDF files get a typed census only (audio is never transcribed). SVG is
markup and is not rasterized. Other bytes get a typed digest and no pixels.

| Flag | Meaning |
|---|---|
| `--fit N` | Longest edge in pixels for an image or frame, or the width of a contact sheet. Clamped to 16-8192. Defaults: 1024 for an overview or frame, 1280 for a crop, 1600 for a sheet. |
| `--grid RxC` | Rows by columns of the labelled grid, each 1-26 (columns are letters). A cell under 24 px cannot be labelled, and an explicit grid that fine is refused. |
| `--cell C` | Crop a grid cell (`B2`) or range (`A1:B2`) from the original. An alternative to `--region`. |
| `--region R` | Crop `x,y,w,h` in pixels, or `x%,y%,w%,h%`, from the original. |
| `--at T` | One video frame (or animated-image frame) at `T`: seconds (`90`) or clock (`1:30` is 90 s). |
| `--range A-B` | A stretch, each side seconds or clock: `2-9` or `1:23-1:30`. `A:B` is refused as ambiguous. |
| `--every S` | A frame every `S` seconds, over the clip or over `--range`. |
| `--count N` | `N` evenly spaced frames, over `--range` or `--sheet`. An alternative to `--every`. |
| `--sheet` | Contact sheet of timestamped frames. The default for video: 12 frames, at most 36. |
| `--scenes` | Timestamps where the picture changes sharply (score threshold 0.30; at most 48 found, 16 listed). Takes only an optional `--range`. |
| `--subs` | Extract an embedded text subtitle track (256 KiB at most, 12 lines shown). Takes no other timeline flag. A bitmap subtitle track is declined. |
| `--json` | The same facts as JSON, schema `ctx.view/v1`: `status`, `target`, `view`, `source`, `derived`, `omitted`, `notes`, `next`, and `read` when a time was given. `derived` carries `tokens` with its `tokens_basis` and `tokens_before` with its `tokens_before_basis`, plus `op`, `mime`, `path`, `address`, `width`, `height`, `bytes`, `engine`, `engine_version`, `source_sha256`, `output_sha256`, `params` and `meta`. An error is also a JSON object. |

`--grid`, `--cell` and `--region` apply to still images. `--at` and the other time
flags apply to video and animated images. `--at` cannot be combined with another
timeline flag.

**Engines.** Pillow (the `image` extra) and ffmpeg are optional. A missing engine
is declared in the output with an install hint and exit status `1`, never a
traceback. With neither, an image still gets its census (format, dimensions, size,
hash, raw token estimate) and a video gets container facts only. Pillow alone
covers every image operation and animated images. ffmpeg without Pillow covers
`--at`, `--scenes` and `--subs` on video; the contact sheet and `--range` need
Pillow to tile and label. The matrix is in
[Pastes and media](PASTES-AND-MEDIA.md#optional-engines-and-what-works-without-each).

Token figures are estimates, not measurements of any model. The image tier comes
from `CTX_MODEL` when it names a known model, otherwise a range across both tiers
is shown and labelled `model unknown`; see
[Pastes and media](PASTES-AND-MEDIA.md#token-figures-are-estimates).

Exit status: `0` a view; `1` declined with a reason (missing engine, an unreadable
or refused file, a limit); `2` a selector combination that cannot mean anything.

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

## Connect a coding harness to the sidecar

Keep using the agent you prefer. `ctx setup --host <name>` configures one
integration; `ctx wrap detect` lists supported hosts. Hermes (`hermes` or
`open-hermes`), Oh My Pi (`omp` or `oh-my-pi`), OpenCode (`opencode`), and
DeepSeek Harness (`dsh`) provide native hook plugins and MCP retrieval/edit tools.
Their wrappers accept agent arguments after `--`; setup persists after exit.
See [Agent integrations](AGENT-INTEGRATIONS.md) for commands and limits.

Codex and OpenCode configuration planning:

```bash
ctx setup --host codex --host opencode --preview   # no writes or receipts
ctx setup --host codex --host opencode --apply     # safe changes only
ctx setup --host codex --repair                   # restore missing owned stages; verify
ctx setup --host codex --remove                   # preview owned removal
ctx setup --host codex --remove --apply
```

Preview and removal require explicit Codex/OpenCode hosts. They cannot be
combined with `--all`, `--acp` or `--prune`; `--preview` and `--apply` are mutually
exclusive. Default setup stops for review before changing a pre-existing user
configuration. Conflicts require reconciliation even with `--apply`.
See [Getting started](GETTING-STARTED.md) for ownership and preservation rules.

`ctx mcp --bounded-only --workspace /absolute/project` serves the bounded MCP
tool with that default workspace. A tool call's explicit `workspace` overrides
the default. The MCP tool does not execute arbitrary commands; use `ctx run`
through the agent's terminal and permission flow.

Add `--with-edits` to expose `ctx_edit` with anchored patch plans, apply receipts,
and structural rewrite previews. This requires `--workspace`; edit calls cannot
override that workspace. The new host integrations and ACP workers enable this
tool. Plain `ctx mcp` retains the retrieval-only surface.

`ctx setup --host <name> --acp --acp-model <exact-agent-model-id>` configures an
ACP worker endpoint alongside the selected host's setup. Optional flags are
`--acp-command '<JSON argv array>'`, `--acp-tier economy|standard|frontier`, and
`--acp-permissions deny|allow_once`. See [ACP orchestration](ACP.md) for endpoint
commands, model negotiation, permission behavior, and transport limits.

### Claude Code

```bash
ctx wrap claude --proxy -- -p "fix the failing tests"
```

The wrapper injects host settings for the session and removes them when the process
ends. With `--proxy` the session's `ANTHROPIC_BASE_URL` points at a loopback
relay; because Claude Code turns deferred tool loading off for any non-Anthropic
base URL, the wrapper also sets `ENABLE_TOOL_SEARCH=true` when the relay's
upstream is Anthropic itself (a value you set yourself is left alone, and a
third-party upstream is never forced). Without that, every request carried the
full tool catalogue — measured at ~15k extra cached tokens per call.

#### Shrink the launch surface: `--surface`

```bash
ctx wrap claude -- -p "fix the failing tests"                # --surface auto is the default
ctx wrap claude --surface off -- -p "…"                      # change nothing
ctx wrap claude --surface read-only -- -p "review this repo" # a named profile
```

Before launch, `ctx wrap claude` computes a capability plan for the session and
applies it only through the channels the wrapper already uses for one session:
the temporary settings file, extra launch flags, and private temporary files.
Nothing is written under your home directory and nothing is left after the
session. The one file written to the workspace is the receipt,
`.ctx-surface/wrap-receipt.json`, schema `ctx.surface.wrap/v1`, and one line on
stderr summarises it:

```text
ctx wrap: surface auto · skills 2/3 name-only (est. ~51→22 tok) · receipt .ctx-surface/wrap-receipt.json
```

| Value | What the launch does |
|---|---|
| `auto` | The default. MCP: relies on Claude Code's own tool deferral and interposes nothing. Skills: a skill with zero invocations in this project's transcripts is set to `name-only` through `skillOverrides`, which keeps its name listed and drops its description. Nothing is turned off, ctx's own skill is never touched, and a skill the user already set in `skillOverrides` is left alone. |
| `off` | Compute and apply nothing; no receipt. |
| `<profile>` | Launch only the MCP servers the profile keeps (`read-only`, `local-dev`, `review`, `full`, or one defined in `ctx.toml` under `[surface.profiles.<name>]`). Passes `--mcp-config` and `--strict-mcp-config`. A profile can only be chosen on this flag: a `ctx.toml` that names one is ignored with a note, because a cloned repository must not choose it for you. |

The default comes from `[surface] wrap` in `ctx.toml`, which may be `auto` or
`off` ([Configuration](CONFIGURATION.md)).
The flag may be given once and takes exactly one value; anything else exits `2`.
It applies when `ctx wrap claude` launches Claude Code, that is, with agent
arguments after `--`; without them the command runs guided setup and plans
nothing. On another host the flag prints a note and is ignored.

`auto` trims a skill only when the evidence covers at least `[surface]
wrap_min_sessions` complete sessions (default 5); one session says nothing about
"never used". A skill newer than the newest transcript, or a project with no
Claude Code transcripts, is left unchanged and the receipt says why. Evidence
comes from the project's own session transcripts under Claude Code's config
directory; `ctx` does not start any MCP server to measure it.

The caller wins. `--mcp-config`, `--strict-mcp-config`, `--settings`, or the
environment variables that control tool deferral and the skill budget
(`ENABLE_TOOL_SEARCH`, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`,
`SLASH_COMMAND_TOOL_CHAR_BUDGET`) leave that dimension alone, and a managed MCP
configuration stops a profile from being applied. The receipt lists each item
left alone with its reason. A profile that restricts individual tools of a server
is enforced through `ctx surface gateway`; when that cannot be enforced (no cached
tool catalogue for the server, or a remote server the gateway does not proxy),
the whole server is left out of the session and the stderr line, the receipt and
the profile note say so and name the remedy. A project server Claude Code would
not start itself is never started. Every token figure in the receipt is an
estimate (about 4 bytes per token). If planning fails, the session launches
unchanged with a one-line note.

### Antigravity

```bash
ctx antigravity install
```

The plugin is persistent. Both hosts use the same artifact store, digest contracts, and
retrieval vocabulary. On `agy` 1.5.5 the plugin rewrites a flooding command in place
instead of refusing it; see [Host capabilities](HOST-CAPABILITIES.md#antigravity-agy-155).

## See what ctx governs: `ctx coverage`

```bash
ctx coverage                       # the table for every host, then what the hooks observed
ctx coverage --host antigravity    # one host dialect
ctx coverage --session last        # observed calls for the newest session only
ctx coverage --json                # machine-readable (schema ctx.coverage-report/v1)
```

Every native tool call an agent makes takes one of a few routes, and ctx declares
which in one table (`src/ctx/governance.py`) that the hook, the installed
registrations and this command all read:

| Route | What happens |
|---|---|
| `rewrite` | PreToolUse replaces the call's input in place; the call runs, as its bounded `ctx` form. For shell commands on Claude Code and Codex this route is not taken by default (`steering = "auto"`): the command runs as written. |
| `bound` | PreToolUse narrows the input (a `limit`), or past the inline budget denies with a map of the file. |
| `deny_steer` | PreToolUse denies and names the `ctx` command to run instead. |
| `output_gate` | PostToolUse replaces an over-budget result with a digest and a retrieval address. Under budget it passes unchanged. |
| `observe` | The hook sees and records the call and never changes it (edits, skills, and every result on a host that cannot replace output). |
| `exempt` | Deliberately not hooked, with a stated reason. |

A tool the table has never seen falls to the output gate where the host can
replace output, and is observed elsewhere. An MCP tool is recognised before any
native name and is governed by the output gate; it is not classified as a native
search or read. `ctx coverage` also prints, per host, what no hook can govern, and
what the prompt gate does for an oversized paste. A route whose stage the installed
registration does not hook is printed as `UNHOOKED`. Excerpt:

```text
antigravity  (rewrite input: read_file, run_command only, replace output: no)
  pre=rewrite     post=observe      run_command
  pre=bound       post=observe      read_file
  pre=deny_steer  post=observe      view_file, grep_search, find_by_name, list_dir, ListDir, glob_search, codebase_search
  pre=observe     post=UNHOOKED     write_to_file, replace_file_content, multi_replace_file_content, edit_file
  …
  prompt (oversized paste): no ctx prompt gate: an oversized prompt reaches the model unmodified
  cannot govern:
    - PostToolUse cannot replace output (its only legal output is {}): the result of a tool that was not rewritten reaches the transcript raw
```

The second half is the coverage ledger, `.ctx-session-reads/coverage.jsonl`
(`ctx.coverage/v1`). The hooks append one row per hooked call: the tool name (for
MCP the server and tool, for a skill its name), the stage, the route, the decision
and the raw and emitted byte counts. It never records arguments or content. It
rotates at 512 KiB and keeps one older generation, so history is bounded.

```text
observed  (.ctx-session-reads/coverage.jsonl: 1 rows, session all, 1 session(s) on record; …)
  host         tool                               stage        route        calls      raw B emitted B  decisions
  claude-code  Bash                               post         output_gate      1      88889       982  replaced=1
  most ungoverned bytes into context (observed, exempt, captured-not-replaced, failed, or a replacement the host ignores):
    none recorded
```

The "most ungoverned bytes" list ranks the tools whose results reached the model
without a bound: observed, exempt, captured without replacement, failed, or a
replacement the host would ignore. It is the place to look when a session was
larger than expected. When `[guard] mode = "advisory"` the report says every
route is switched off in this workspace. When `mode = "strict"` it says strict has
no code path of its own and behaves as `guarded`. When the workspace's Claude Code
settings carry a ctx matcher older than the table, it says so; `ctx setup`
updates ctx-owned entries in place and leaves customised ones alone.

Flags: `--json`; `--host` with one of `claude-code`, `codex`, `antigravity`,
`hermes`, `omp`, `opencode`, `dsh`; `--session ID` or `last`. The command always
exits `0`: it reports, it does not judge.

## Score a trajectory against gold context: `ctx replay --gold`

```bash
python evals/contextbench.py --workdir /scratch/cb --limit 40 --stratify \
    --emit-gold /scratch/gold
ctx replay --gold /scratch/gold/<instance>.json ~/.claude/projects/*/<session>.jsonl
```

Reconstructs the file regions an agent actually opened during a recorded
session and scores them against human-annotated gold at file, block and line
granularity, plus evidence density and regret against the gold oracle. No model
and no network.

The extractor reads the transcript and nothing else, so it scores a session
that used ctx and one that did not through the same instrument. That is the
point: an A/B whose arms are measured differently has decided its result before
it runs. Design and pre-registered predictions:
[the A/B design](../evals/contextbench-ab-design.md).

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
| `3` | **The thing you asked about** failed | ctx worked; the child command, script, sequence step, or job exited nonzero. The digest is the evidence — read it, do not re-run the command to see the output. Exception: `ctx run --passthrough` (hook mode, the form the hooks rewrite a tool call into where a rewrite happens: Antigravity, and Claude Code with `steering = "rewrite"`) exits with the child's own status, and prints small, complete output verbatim with the `run:` handle on one trailing line, so a rewritten tool call looks to the host like the native one. Hooks pass the original command verbatim as `ctx run --passthrough --shell -- '<original>'` |
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
