# Getting started

[Documentation](README.md) · [How it works](HOW-IT-WORKS.md) · [Troubleshooting](TROUBLESHOOTING.md)

Install Straitjacket, capture one command, and retrieve its evidence. Configure
a coding-agent host after the terminal workflow works.

## Requirements

- Python 3.11 or newer.
- A local repository.
- A supported coding-agent host if you want automatic interception.

Host integration is optional. Every `ctx` command also works from a terminal.

`rg`, ctags, tree-sitter, Jedi, SCIP, and Semgrep can improve repository analysis. Capture and retrieval do not depend on them.

## Know the data boundary

Captured stdout and stderr may contain source code, logs, credentials, or other
sensitive data. Raw artifacts are stored as local plaintext today.
Model-visible output is deterministically redacted by default, but redaction
can be disabled explicitly; it does not encrypt the stored artifact.

Host-hook reads of secret-bearing paths require an explicit permission step and
are excluded from automatic capture by default. A directly authorized
`ctx run` can still capture them.

The store lives outside the repository by default under `$CTX_STATE_HOME`,
then `$XDG_STATE_HOME/ctx`, then `~/.local/state/ctx`. The default retention
GC eligibility horizon is 30 days. There is no background collector: artifacts
remain until `ctx gc` runs. `ctx gc --retention-days N` can override the
horizon; pins and active checkpoint leases survive collection.

Do not capture secret-bearing commands when plaintext local retention is
unacceptable.

## Install

```bash
python -m pip install --upgrade ctx-harness
ctx --version
```

Isolated installations also work:

```bash
pipx install ctx-harness
# or
uv tool install ctx-harness
```

For development:

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e '.[dev]'
pytest
```

## Run the first capture

```bash
ctx run -- pytest -q
```

Small output may pass through unchanged. Large output becomes a digest:

```text
[ctx run:ba3d1020ee8f profile=pytest/v2]
exit: 1
failures: 7
coverage:
  identities: 7/7
  detail shown: 2/7
next:
  ctx get run:ba3d1020ee8f#stdout --lines 140:220
```

The handle names the complete stored artifact. The digest names what it omitted and how to retrieve it.

## Retrieve and search

Retrieve an exact line range:

```bash
ctx get run:ba3d1020ee8f#stdout --lines 140:220
```

Search the stored output:

```bash
ctx search run:ba3d1020ee8f "MissingTenantError"
```

Use `#stdout` or `#stderr` to select a stream. Use `--lines`, `--bytes`, or a span address to select a region.

Retrieval remains bounded. If the requested region is too large, `ctx get` returns a smaller view with continuation addresses.

## Know the host boundary

| Host | Birth-time containment | Oversized result substitution |
|---|---|---|
| Claude Code | Transparent rewrite | Yes |
| Codex | Implemented and contract-tested | Implemented and contract-tested; live CLI receipt pending |
| Antigravity | Deny with a bounded replacement command | No |

Antigravity's published PreToolUse contract cannot modify arguments. Its
PostToolUse contract cannot replace tool output. straitjacket can prevent a
known command flood before execution, but a verbose connector result can still
enter the transcript unchanged.

See [Host capabilities](HOST-CAPABILITIES.md) for the full matrix.

## Configure the repository

```bash
cd your-repository
ctx setup
ctx doctor
```

`ctx setup`:

1. creates or updates `ctx.toml` and `.ctxignore`;
2. detects supported agent CLIs;
3. applies host configuration using host-specific preservation rules;
4. runs the same checks as `ctx doctor`;
5. records a content-free fingerprint of the managed setup surfaces for fast
   reruns.

It preserves unrelated user-owned settings. It merges JSON settings where safe,
refreshes ctx-managed files and marker-delimited blocks, and refuses to rewrite
user-owned Codex TOML. A matching readiness receipt can skip repeated writes;
use `ctx setup --repair` to force all checks and repair. Use `ctx setup --all` to
prepare all three vendor hosts; `antigravity-sdk` remains an explicit opt-in.

Setup reports host-configuration writes. The persistent host configuration is:

| Scope | Files or entries |
|---|---|
| Workspace | `ctx.toml`, `.ctxignore` |
| Antigravity | `.agents/plugins/ctx-harness/`; a ctx `statusLine` in `~/.gemini/antigravity-cli/settings.json` only when no status line already exists |
| Claude Code | ctx hook entries and, when absent, a ctx `statusLine` in `.claude/settings.json`; `.claude/agents/ctx-explorer.md` only when absent; a managed block in `CLAUDE.md` |
| Codex | ctx entries in `.codex/config.toml` and `.codex/hooks.json`, a managed block in `AGENTS.md` |

Setup also writes a content-free readiness receipt to
`.ctx-session-reads/setup.json`; this is not host configuration.

If a user-owned Codex configuration cannot be merged safely, setup prints the
MCP table instead of replacing the file. Add that table, and ensure the existing
`[features]` table contains `hooks = true` (create the table if absent; do not
duplicate it). Then rerun `ctx setup`. Preview one host without writing:

```bash
ctx wrap codex --print-config
```

To remove the integration, remove only the ctx-managed directory, entries, and
marker-delimited blocks. Keep unrelated settings. The exact host-by-host steps
are in [Troubleshooting](TROUBLESHOOTING.md#how-do-i-turn-the-harness-off-or-uninstall-it).

Configure one host only:

```bash
ctx wrap antigravity
ctx wrap claude
ctx wrap codex
```

## Choose the command by work shape

| Work | Command |
|---|---|
| One command | `ctx run -- <command>` |
| Pipeline or shell syntax | `ctx run --shell '<pipeline>'` |
| Known sequence | `ctx seq` |
| Computed branching or aggregation | `ctx py <script>` |
| Typed bounded composition | `ctx q '<pipeline>'` |
| Long-running work | `ctx run --bg-after <seconds> -- <command>` |

Use the least expressive option. Batch operations whose sequence is already known. Return to the model when evidence can change the plan.

## Long-running commands

```bash
ctx run --bg-after 30 -- pytest tests/integration -q
```

If the process is still running after 30 seconds, `ctx` returns a `job:` handle and continues capturing in the background.

```bash
ctx job <id>
ctx job <id> --wait
ctx job <id> --kill
```

A completed job becomes an ordinary `run:` artifact.

## Repository questions

Build a bounded repository map:

```bash
ctx map --budget 500 --focus payments
```

Ask a typed question:

```bash
ctx ask "Where is TokenBucket defined and used?" --intent locate
```

Run a compiled investigation:

```bash
ctx plan run investigation.json
```

Use `ctx q` for bounded pipelines over typed facts:

```bash
ctx q 'fails last | in-changed'
ctx q 'refs TokenBucket | group file | top 5'
```

`ctx q` has bounded stage counts and no recursion or unbounded loops. This keeps execution cost predictable.

## Ephemeral host session

Run Claude Code under the harness without changing persistent configuration:

```bash
ctx wrap claude -- -p "fix the failing tests"
```

Add `--proxy` to measure wire traffic through the local observer:

```bash
ctx wrap claude --proxy -- -p "fix the failing tests"
```

The observer records usage and window metadata. It does not record request bodies or authentication headers.

## Inspect the result

```bash
ctx stats --session
ctx gain
```

The scorecard reports captured versus model-visible bytes, profile use, retrievals, reruns, cache classes, and intervention outcomes. Treat it as a diagnostic receipt.

## Trust boundary

straitjacket provides:

- output containment;
- deterministic rendering;
- bounded retrieval;
- workspace-relative confinement for ctx-managed repository reads and searches;
- traversal and symlink-escape rejection on those ctx-managed paths;
- timeouts and process-group handling;
- secret-aware controls.

It is not a complete process sandbox. Child processes launched through
`ctx run` keep the invoking user's filesystem and execution authority.

## Next

- [CLI guide](CLI.md)
- [Configuration](CONFIGURATION.md)
- [Core concepts](CONCEPTS.md)
- [Architecture](ARCHITECTURE.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Evaluation receipts](../evals/)
