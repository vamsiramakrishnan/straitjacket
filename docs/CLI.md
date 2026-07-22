# CLI guide

The Straitjacket command is `ctx`.

```text
ctx [--workspace PATH] <command> [options]
```

Use `--workspace` when the current directory is not the workspace you want to operate on.

This guide is organized by task. It is not a duplicate of `ctx --help`; it explains which command to choose and the contract each command provides.

## Command chooser

| Need | Command |
|---|---|
| Configure a workspace | `ctx wrap setup` |
| Preview host configuration | `ctx wrap <host> --print-config` |
| Validate the installation | `ctx doctor` |
| Capture one command | `ctx run -- <command>` |
| Capture a shell pipeline | `ctx run --shell '<pipeline>'` |
| Run known steps in one round | `ctx seq '<step 1>' '<step 2>'` |
| Run computed control flow | `ctx eval <script>` |
| Preview or apply a structural rewrite | `ctx rewrite <pattern> <replacement>` |
| Supervise long-running work | `ctx run --bg-after <seconds> -- <command>` and `ctx job` |
| Retrieve exact evidence | `ctx get <ref>` |
| Search an artifact or repository | `ctx search <ref> <pattern>...` |
| Inspect repository structure | `ctx map`, `ctx def`, `ctx refs`, `ctx callers`, `ctx callees`, `ctx impact`, `ctx diag` |
| Compose typed evidence | `ctx q '<pipeline>'` |
| Compile a repository question | `ctx ask "<question>" --intent <intent>` |
| Validate or run an evidence plan | `ctx plan` and `ctx investigate` |
| Compare two captured runs | `ctx diff run:<before> run:<after>` |
| Inspect context economics | `ctx stats --session` and `ctx gain` |
| Replay recorded behavior | `ctx replay` |
| Manage artifact retention | `ctx pin`, `ctx gc`, `ctx checkpoint` |

## Workspace setup

### `ctx init`

Write the baseline workspace files:

```bash
ctx init
```

Use this when you want `ctx.toml` and `.ctxignore` without installing a host integration.

### `ctx wrap`

Configure one or more coding-agent hosts.

```bash
ctx wrap setup
ctx wrap antigravity
ctx wrap claude
ctx wrap codex
```

`setup` and `all` configure Antigravity, Claude Code, and Codex in the current workspace.

Preview the exact host configuration without writing it:

```bash
ctx wrap codex --print-config
```

Run Claude Code ephemerally:

```bash
ctx wrap claude -- -p "fix the failing tests"
```

Measure the true Anthropic wire traffic for that child session:

```bash
ctx wrap claude --proxy -- -p "fix the failing tests"
```

The proxy is optional and fail-open. It is required for `ctx stats --session` because that scorecard uses recorded wire observations.

### `ctx doctor`

Validate workspace, store, and integration health:

```bash
ctx doctor
ctx doctor --antigravity
```

Use `--antigravity` to include workspace-plugin checks.

## Capture and execution

### `ctx run`

Capture one command before its output can flood the transcript.

```bash
ctx run -- pytest -q
ctx run -- ruff check .
ctx run -- git diff --stat
```

Syntax:

```text
ctx run [--focus TEXT] [--cwd PATH] [--timeout SECONDS]
        [--bg | --bg-after SECONDS] [--shell] -- <command>
```

The command produces:

- an immutable run artifact containing complete stdout and stderr;
- a bounded digest selected by the detected evidence profile.

`--` ends Straitjacket's options. Everything after it is passed to the child process.

#### Shell mode

Use shell mode only when shell semantics are required:

```bash
ctx run --shell 'rg -n "TODO" src | sort | head -200'
```

Prefer direct argument execution for one command. It avoids quoting ambiguity and gives the harness a clearer command identity.

#### Working directory

Run relative to a directory inside the workspace:

```bash
ctx run --cwd services/payments -- pytest -q
```

#### Evidence focus

Bias evidence selection toward a question without changing the stored artifact:

```bash
ctx run --focus "authentication failures" -- pytest -q
```

### `ctx seq`

Run a known sequence of shell command strings in one model round.

```bash
ctx seq \
  'git diff --stat HEAD~1' \
  'pytest -q tests/unit' \
  'ruff check src tests'
```

Options:

```text
--keep-going       Continue after a failed step
--timeout SECONDS  Per-step timeout
--focus TEXT       Bias the combined digest
```

Use `ctx seq` when all steps are known before execution and each step should retain its own evidence identity.

Do not use the obsolete `--step` form. Steps are positional command strings.

### `ctx eval`

Run a Python evidence program under the same birth-time capture boundary.

```bash
ctx eval investigation.py
ctx eval --file tools/investigate.py
```

Read the script from standard input:

```bash
ctx eval - <<'PY'
from pathlib import Path
print(sum(1 for _ in Path("src").rglob("*.py")))
PY
```

Use `ctx eval` when the workflow requires branching, loops, or aggregation. The script and intermediate output remain addressable. Only the bounded result enters context.

`ctx eval` is not an operating-system sandbox. It runs with the authority of the invoking user.

### `ctx rewrite`

Preview a structural multi-file rewrite:

```bash
ctx rewrite 'old_call($A)' 'new_call($A)' --lang py --glob 'src/**/*.py'
```

Apply the rewrite only after reviewing the preview:

```bash
ctx rewrite 'old_call($A)' 'new_call($A)' --lang py --glob 'src/**/*.py' --apply
```

The default is preview-only.

### Background work: `--bg`, `--bg-after`, `ctx job`, and `ctx jobs`

Background immediately:

```bash
ctx run --bg -- ./scripts/integration-test
```

Stay in the foreground for a bounded period, then background if still running:

```bash
ctx run --bg-after 30 -- ./scripts/integration-test
```

Inspect or control a job:

```bash
ctx job <job-id>
ctx job <job-id> --tail 100
ctx job <job-id> --wait
ctx job <job-id> --wait --timeout 300
ctx job <job-id> --kill
ctx jobs
```

A completed job finalizes into an ordinary `run:` artifact.

## Retrieval

### Reference types

Straitjacket uses two broad address spaces.

#### Live repository references

```text
repo:
repo:src/auth.py
repo:services/payments
ws:api/repo:src/main.py
```

Repository references resolve against current workspace state. Reads are snapshotted when materialized.

#### Immutable artifact references

```text
run:<id>
run:<id>#stdout
run:<id>#stderr
blob:<id>
snapshot:<id>
checkpoint:<id>
job:<id>
```

Artifact handles identify stored evidence.

### `ctx get`

Retrieve an exact bounded selection:

```bash
ctx get run:<id>#stdout --lines 120:180
ctx get run:<id>#stderr --bytes 0:4096
ctx get blob:<id> --json-pointer /results/0
ctx get snapshot:<id> --symbol AuthContext.resolve
ctx get run:<id>#stdout --span <span-id>
```

Selectors:

```text
--lines A:B
--bytes A:B
--records A:B
--json-pointer POINTER
--symbol DOTTED_NAME
--span SPAN_ID
```

Small selections return exact bytes. Large selections return a bounded zoom digest with narrower addresses.

### `ctx search`

Search an artifact:

```bash
ctx search run:<id>#stdout "MissingTenantError"
ctx search run:<id>#stdout "tenant" "permission" --context 3
```

Search the repository:

```bash
ctx search repo: "MissingTenantError" --glob "**/*.py" --context 3
```

Useful options:

```text
--fixed          Treat patterns as fixed strings
--all            Require all patterns per target
--context N      Include N surrounding lines
--glob PATTERN   Restrict repository paths
--scope NAME     Use a named monorepo scope from ctx.toml
--max-matches N  Bound the result count
```

The syntax always starts with a reference: `ctx search <ref> <pattern>...`.

## Repository analysis

### `ctx map`

Render a ranked, budget-fitted repository map:

```bash
ctx map
ctx map --budget 800 --focus payments
```

### `ctx def`

Locate a symbol definition:

```bash
ctx def repo:src/auth.py:AuthContext.resolve
```

### `ctx refs`

Find reference sites:

```bash
ctx refs AuthContext
ctx refs AuthContext.resolve --path src
```

The active reference engine is disclosed. The engine ladder can use SCIP, Jedi, or a built-in fallback depending on available data and dependencies.

### `ctx callers`, `ctx callees`, and `ctx impact`

```bash
ctx callers AuthContext.resolve
ctx callees AuthContext.resolve
ctx impact AuthContext.resolve --depth 4
```

Use `impact` for a bounded transitive caller analysis. The maximum supported depth is six.

### `ctx diag`

Produce a bounded syntax and diagnostic view:

```bash
ctx diag
ctx diag src/auth
```

## Evidence composition

### `ctx q`

Compose bounded operations over typed evidence streams:

```bash
ctx q 'fails last | in-changed'
ctx q 'refs TokenBucket | group file | top 5'
ctx q 'corpus --ext py --changed | outline'
ctx q 'records run:<id>#stdout --jsonl | group level | count'
```

Add stage provenance:

```bash
ctx q 'refs TokenBucket | group file | top 5' --trace
```

The query algebra is total and bounded: no recursion, no unbounded loops, and a fixed stage budget. Use `ctx q` when the work is a composition of repository and evidence facts. Use `ctx eval` when the control flow is genuinely computational.

### `ctx ask`

Compile a repository question into a typed evidence plan.

```bash
ctx ask "Where is AuthContext defined and used?" --intent locate --symbol AuthContext
ctx ask "What could break if CacheKey.build changes?" --intent impact --symbol CacheKey.build
ctx ask "Why did the last test run fail?" --intent diagnose --run run:<id>
ctx ask "Trace calls from Router.dispatch" --intent trace --symbol Router.dispatch
ctx ask "What changed between these runs?" --intent compare --run run:<a> --against run:<b>
ctx ask "Verify the current change" --intent verify --command 'python -m pytest -q'
ctx ask "Review the current change" --intent review --command 'python -m pytest -q'
```

Supported intents:

| Intent | Purpose | Execution class |
|---|---|---|
| `locate` | Find a symbol and its use sites | Observe |
| `impact` | Estimate structural blast radius | Observe |
| `diagnose` | Explain captured failures without rerunning them | Observe |
| `trace` | Follow structural call paths | Observe |
| `compare` | Compare two captured runs | Observe |
| `verify` | Select and run verification work | Execute |
| `review` | Inspect changes, related symbols, tests, and counterevidence | Execute |

Preview the compiled plan without running it:

```bash
ctx ask "What could break if CacheKey.build changes?" \
  --intent impact --symbol CacheKey.build --plan
```

The intent should be explicit. Straitjacket may suggest a missing intent, but it does not guess and execute an ambiguous plan.

### `ctx plan`

Validate, price, or run a `ctx.plan/v1` document:

```bash
ctx plan validate plan.json
ctx plan price plan.json
ctx plan price plan.json --value
ctx plan run plan.json
ctx plan ops
```

`validate` checks boundedness and capabilities. `price` reports the planned work before execution. `run` executes the DAG and returns one investigation digest.

### `ctx investigate`

Execute one hypothesis epoch from a plan:

```bash
ctx investigate plan.json
ctx investigate plan.json --replans 1
ctx investigate plan.json --advise
```

`--advise` reports a shadow comparison between the declared operator order and the order suggested by recorded follow-up evidence. It does not silently reorder or suppress work.

## Comparison and measurement

### `ctx diff`

Compare two captured runs directly:

```bash
ctx diff run:<before> run:<after>
```

The result focuses on behavioral changes: exits, failures, templates, signals, and stream sizes. It avoids asking the model to compare two complete outputs manually.

### `ctx stats`

Inspect a repository or artifact shape:

```bash
ctx stats repo:src/ctx
ctx stats run:<id>#stdout
```

Render the current session scorecard:

```bash
ctx stats --session
```

The session scorecard requires observations captured by `ctx wrap claude --proxy`.

### `ctx gain`

Show cumulative containment savings by operation family:

```bash
ctx gain
```

Treat `gain` as an accounting view, not a quality score. Pair savings with evidence preservation and task success.

### `ctx replay`

Run deterministic analysis over recorded transcripts:

```bash
ctx replay session.jsonl
ctx replay --all-projects
ctx replay session.jsonl --regret
ctx replay session.jsonl --outcomes
ctx replay session.jsonl --outcomes --append-ledger
```

`--regret` evaluates the distance between an emitted digest and an evidence frontier. `--outcomes` reports observable follow-up behavior. These are offline analyses; runtime does not rewrite committed policy automatically.

## Artifact and task lifecycle

### `ctx checkpoint`

Create a durable task checkpoint:

```bash
ctx checkpoint \
  --goal "fix token expiry handling" \
  --state "failure reproduced" \
  --decision "preserve existing refresh semantics" \
  --evidence "run:<id>#stdout failing traceback"
```

Render an existing checkpoint:

```bash
ctx checkpoint --show checkpoint:<id>
```

### `ctx pin` and `ctx gc`

Protect an artifact from collection:

```bash
ctx pin run:<id>
```

Run mark-and-sweep collection:

```bash
ctx gc
ctx gc --retention-days 30
```

### `ctx debt`

Record an explicitly deferred engineering decision:

```bash
ctx debt add "defer Windows process-group parity" --ref repo:src/ctx/execution.py
ctx debt list
ctx debt resolve <id>
```

### `ctx policy`

Compile and inspect reviewable steering policy:

```bash
ctx policy compile
ctx policy compile --plan-value
ctx policy show
```

Runtime observations may feed the compiler, but runtime does not silently edit the committed policy file.

## Advanced surfaces

### `ctx surface`

Audit and reduce the model-visible capability surface:

```bash
ctx surface inventory
ctx surface audit
ctx surface explain <capability-id>
ctx surface graph
ctx surface compile --profile read-only --host claude
ctx surface reconcile --intent "review the current change"
```

Use `ctx surface --help` for the complete advanced surface workflow.

### `ctx proxy`

Run the Anthropic observer proxy directly:

```bash
ctx proxy \
  --port 8765 \
  --upstream https://api.anthropic.com \
  --state-dir .ctx-session-reads/proxy
```

Most users should prefer `ctx wrap claude --proxy`, which scopes the proxy environment to the child process and shuts it down with the session.

## Failure semantics

Straitjacket separates hard containment from optional intelligence.

- Safety gates fail closed when allowing an operation could violate a hard containment rule.
- Optional extractors and accelerators fall back to a labeled lower-precision mode.
- Omission is declared and addressable.
- Internal hook failures follow the configured policy and must still emit a valid host decision.

## Output contract

A model-visible result should answer:

```text
What happened?
What evidence supports it?
What was omitted?
How complete is the view?
Which exact address retrieves the next useful detail?
```

Renderers and engines may evolve. Boundedness, determinism, declared coverage, and addressability are compatibility properties.

---

[Documentation](README.md) · [Getting started](GETTING-STARTED.md) · [Use cases](USE-CASES.md) · [Core concepts](CONCEPTS.md)
