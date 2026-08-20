# Getting started

<sub><a href="README.md">« straitjacket / docs</a></sub>

This guide gets you from a checkout to one harnessed coding-agent session, then shows the three operations that make the rest of straitjacket understandable: **capture, inspect, retrieve**.

## Before you begin

straitjacket currently requires:

- Python 3.11 or newer;
- a local repository workspace;
- Antigravity, Claude Code, or Codex for automatic host integration
  (or none — every capture/retrieval verb also works standalone);
- optional binaries such as `rg` and `ctags` for richer repository analysis.

The Python core remains deliberately small. Optional analysis backends improve precision or speed, but their absence should degrade capability rather than break capture.

For image render comparison, install the optional Pillow decoder:

```bash
python -m pip install -e '.[image]'
```

Magic-byte detection, exact hashing, common image dimensions, and labelled PDF
structure remain available without it.

## Install and set up

```bash
python -m pip install --upgrade ctx-harness
ctx --version
cd your-repo
ctx setup
```

`ctx-harness` is published on PyPI; it installs the `ctx` command. `pipx install
ctx-harness` and `uv tool install ctx-harness` are equivalent isolated-tool
installs. Contributors who need the unreleased source version can use an
editable checkout:

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e '.[dev]'
ctx --version
```

`ctx --version` should match the wheel name. The release build is smoke-tested
outside the checkout, including all three host renderers and the managed
Antigravity SDK shim.

`ctx setup` writes the workspace configuration (`ctx.toml`,
`.ctxignore`) and installs the harness for each detected host it supports —
Antigravity, Claude Code, and Codex — in one idempotent command. It detects the
CLIs already on `PATH`; if none are found, it prepares all three configurations
for later. Existing
host config is merged, never clobbered; re-running is a no-op. Verify:

```bash
ctx doctor                      # validate the install, store, hooks, and classifier
ctx doctor --antigravity        # also validate the Antigravity plugin files
ctx wrap codex --print-config   # preview any host's config without writing
```

After a successful doctor pass, setup records a privacy-safe fingerprint of the
managed configuration. An unchanged repeat takes a tiny verified no-op path;
an upgrade, failed prior setup, selected-host change, or config drift
automatically runs the idempotent installers and doctor checks again. Use
`ctx setup --repair` to bypass the receipt deliberately, or `ctx setup --all`
to prepare every supported host before its CLI is installed. The receipt stores
hashes and counters, never configuration contents or paths.

Each check prints a `✓` or `✗`; a non-zero exit means something needs
attention. [Troubleshooting](TROUBLESHOOTING.md) explains every failing check.

Per host, what setup writes:

| Host | Files | Enforcement |
|---|---|---|
| Antigravity | `.agents/plugins/ctx-harness/` + native lifecycle hook registration | transparent birth gate; no output-side gate |
| Claude Code | `.claude/settings.json` hooks + explorer agent | fully enforced (birth + output) |
| Codex | `.codex/config.toml` + `.codex/hooks.json` + `AGENTS.md` block | fully enforced (birth + output) |

There are two honest per-host differences, both traceable to what each host's
published hook contract actually permits.

**The birth gate fires everywhere.** Claude Code and Codex use `updatedInput`;
current Antigravity uses `overwrite`. All three transparently turn `pytest -q`
into `ctx run -- pytest -q`, so the flood is prevented without spending a retry
turn.

**The output-side safety net does not exist on Antigravity.** The PostToolUse
gate that replaces an oversized tool result with a digest needs a host API that
can substitute a tool's output: Claude Code (`updatedToolOutput`) and Codex
(`continue:false` + `stopReason`) have a textual path. Codex substitutes tools
with a registered string-return contract—including `webrun`—without rejecting
the nested promise. Unknown structured arrays/objects are captured but passed
through unchanged because code-mode callers may consume their shape and
`updatedMCPToolOutput` is not supported yet.
Antigravity's PostToolUse contract
permits exactly one output — `{}` — so the hook there can neither replace a
result nor attach a nudge. It stays **observational**: the bytes are still
captured into the store (so `ctx get` resolves them later), but nothing shrinks
what already reached the transcript. Practically: a verbose **MCP/connector
result** lands in full on Antigravity. Mitigation: use the bounded `ctx` MCP
tool (`ctx search`/`get`/`stats`) for retrieval, which is capped by
construction.

Prefer a single host? `ctx wrap antigravity`, `ctx wrap claude`, or
`ctx wrap codex` do exactly one. (For Antigravity, `ctx wrap antigravity` renders
and installs the plugin — the same thing `ctx antigravity install` does directly,
which is why `ctx doctor` refers to that lower-level command when the plugin is
missing.)

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
  ctx get run:ba3d1020ee8f#stdout --lines 140:220
```

The handle identifies immutable evidence. The digest is a view over that evidence, not a replacement for it. Every digest ends with a ready-made `next:` retrieval command — you rarely have to construct one yourself.

### 2. Retrieve the exact region you need

Run the `next:` command the digest gave you, or address a stream by line range yourself:

```bash
ctx get run:ba3d1020ee8f#stdout --lines 140:220
```

A stream is addressed by `#stdout` or `#stderr` plus a selector (`--lines`, `--span`, `--bytes`). The digest also mints span tokens you can retrieve directly with `ctx get <span-id>`.

Large retrievals remain bounded. A broad request returns a smaller zoom digest with further addresses rather than reflooding the transcript.

### 3. Search captured evidence

```bash
ctx search run:ba3d1020ee8f "MissingTenantError"
```

Search operates over stored evidence. It does not rerun the original command and does not require the whole artifact to re-enter the model context.

### 4. Understand the repository without reading it into context

Capture, retrieve, and search are the core loop. Three more verbs let an agent
understand a codebase without `cat`-ing files into the transcript — each returns
a bounded, priced view:

```bash
ctx map --budget 500 --focus payments      # a ranked, token-budgeted map of the repo
ctx ask "Where is TokenBucket defined and used" --intent locate
ctx plan run investigation.json            # run a compiled multi-step investigation, get ONE digest
```

`ctx ask` answers a repository question through a typed **intent** (seven ship:
`locate`, `impact`, `diagnose`, `trace`, `compare`, `verify`, `review`) and
`ctx plan`/`ctx plan run` collapse a multi-round investigation into a single
local pass. Full detail is in the [CLI guide](CLI.md).

## Choose the right capture verb

Use the least powerful operation that can express the work:

| Work shape | Use |
|---|---|
| one command | `ctx run -- <command>` |
| shell syntax or a pipeline | `ctx run --shell '<pipeline>'` |
| a known sequence of operations | `ctx seq` |
| computed branching, loops, or aggregation | `ctx py` |
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
- Keep the [CLI guide](CLI.md) handy for the full verb reference.
- Tune budgets, the guard, and scopes in the [Configuration reference](CONFIGURATION.md).
- Hit a snag? [Troubleshooting & FAQ](TROUBLESHOOTING.md) is symptom → cause → fix.
- Read [the documentation map](README.md) for the architecture sequence.
- Inspect [`evals/`](../evals/) to reproduce the measured claims.
- Use [`spec/`](../spec/) when you need normative schemas or compatibility contracts.
