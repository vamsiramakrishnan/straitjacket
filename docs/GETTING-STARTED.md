# Getting started

<sub><a href="index.md">« documentation</a></sub>

This guide gets you from a checkout to one harnessed coding-agent session, then shows the three operations that make the rest of straitjacket understandable: **capture, inspect, retrieve**.

## Before you begin

straitjacket currently requires:

- Python 3.11 or newer;
- a local repository workspace;
- Antigravity, Claude Code, or Codex for automatic host integration
  (or none — every capture/retrieval verb also works standalone);
- optional binaries such as `rg` and `ctags` for richer repository analysis.

The Python core remains deliberately small. Optional analysis backends improve precision or speed, but their absence should degrade capability rather than break capture.

## Install and set up (one command)

```bash
python -m pip install -e .
cd your-repo
ctx wrap setup
```

`ctx wrap setup` writes the workspace configuration (`ctx.toml`,
`.ctxignore`) and installs the harness for every host it supports —
Antigravity, Claude Code, and Codex — in one idempotent command. Existing
host config is merged, never clobbered; re-running is a no-op. Verify:

```bash
ctx doctor --antigravity    # 15 health checks
ctx wrap codex --print-config   # preview any host's config without writing
```

Per host, what setup writes:

| Host | Files | Enforcement |
|---|---|---|
| Antigravity | `.agents/plugins/ctx-harness/` (MCP tool + hooks) | enforced |
| Claude Code | `.claude/settings.json` hooks + explorer agent | enforced |
| Codex | `.codex/config.toml` + `.codex/hooks.json` + `AGENTS.md` block | enforced |

Prefer a single host? `ctx wrap antigravity`, `ctx wrap claude`, or
`ctx wrap codex` do exactly one.

## Or: one ephemeral session, zero residue

```bash
ctx wrap claude --proxy -- -p "fix the failing tests"
```

The `--` form injects host settings for this process only and removes them
when the session exits — nothing persistent is left behind. (`--proxy`
additionally routes API traffic through a localhost observer that measures
the session's true wire cost; optional.)

During the session, operations that could produce unbounded output are captured and replaced with bounded evidence digests. Small outputs may still pass through whole when containment would add no value.

After the session:

```bash
ctx stats --session
ctx gain
```

`ctx stats --session` explains what crossed the wire and how the session behaved. `ctx gain` shows cumulative containment savings by operation family.

## Learn the core loop manually

Automatic steering is useful, but the product becomes clearer when you run the core loop yourself.

### 1. Capture a noisy command

```bash
ctx run -- pytest -q
```

straitjacket stores complete stdout and stderr, then returns either the original small result or a bounded digest. A large test run might look conceptually like:

```text
[ctx run:ba3d1020ee8f profile=pytest/v2]
command: pytest -q
exit: 1
failures: 7
...
coverage:
  identities: 7/7
  detail shown: 2/7
next:
  ctx get run:ba3d1020ee8f#failure-3
```

The handle identifies immutable evidence. The digest is a view over that evidence, not a replacement for it.

### 2. Retrieve the exact region you need

```bash
ctx get run:ba3d1020ee8f#failure-3
```

For line-addressed streams:

```bash
ctx get run:ba3d1020ee8f#stdout --lines 140:220
```

Large retrievals remain bounded. A broad request returns a smaller zoom digest with further addresses rather than reflooding the transcript.

### 3. Search captured evidence

```bash
ctx search run:ba3d1020ee8f "MissingTenantError"
```

Search operates over stored evidence. It does not rerun the original command and does not require the whole artifact to re-enter the model context.

## Choose the right capture verb

Use the least powerful operation that can express the work:

| Work shape | Use |
|---|---|
| one command | `ctx run -- <command>` |
| shell syntax or a pipeline | `ctx run --shell '<pipeline>'` |
| a known sequence of operations | `ctx seq` |
| computed branching, loops, or aggregation | `ctx eval` |
| bounded composition over typed evidence | `ctx q` |
| work that may outlive the turn | `--bg-after` and `ctx job` |

A useful rule: **batch deterministic fan-out, not uncertainty**. Use `seq`, `eval`, or `q` when the next operations are already knowable. Return to the model when new evidence could change the hypothesis.

## Work with long-running commands

```bash
ctx run --bg-after 30 -- pytest tests/integration -q
```

If the command completes within 30 seconds, the result is identical to a normal foreground capture. If it runs longer, the transcript receives a `job:` handle while output continues to spool into the store.

```bash
ctx job <job-id>
ctx job <job-id> --wait
ctx job <job-id> --kill
```

A completed job finalizes into an ordinary `run:` artifact.

## Ask bounded questions over repository evidence

`ctx q` composes typed stages without arbitrary code execution:

```bash
ctx q 'fails last | in-changed'
ctx q 'refs TokenBucket | group file | top 5'
ctx q 'decls auth | where kind=function | count'
```

Queries are deliberately total: bounded stage count, no recursion, no unbounded loops. That makes their cost statically constrainable and their intermediate results addressable.

## Read the session scorecard

After a harnessed session:

```bash
ctx stats --session
```

The scorecard may include:

- model-visible versus captured bytes;
- command families and digest profiles;
- equivalent reruns and successful retrievals;
- window pressure and rescue activity;
- intervention outcomes;
- prompt-cache classes;
- opportunities where deterministic work could have been collapsed.

Treat the scorecard as an engineering receipt, not a vanity dashboard. Its purpose is to identify the next mechanism or policy change from observed behaviour.

## Understand the trust boundary

straitjacket currently provides:

- output containment;
- deterministic evidence rendering;
- repository-relative path confinement;
- symlink and traversal rejection;
- command timeouts and process-group handling;
- bounded retrieval and emission.

It is not yet a complete process sandbox. Commands execute with the authority of the invoking user. Capability handles, a separately owned artifact store, and broker-grade process isolation are planned as a distinct security boundary.

## Where to go next

- Read [Core concepts](CONCEPTS.md) for the vocabulary used throughout the project.
- Read [the documentation map](README.md) for the architecture sequence.
- Inspect [`evals/`](../evals/) to reproduce the measured claims.
- Use [`spec/`](../spec/) when you need normative schemas or compatibility contracts.
