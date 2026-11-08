# Getting started

**Straitjacket is a context and evidence sidecar for coding agents.** It adds
bounded tool output and retrievable evidence to an existing coding harness.

Keep using Claude Code, Codex, Antigravity, or your preferred coding agent.
Straitjacket runs alongside it. Your agent still handles the conversation,
model selection, authentication, and permissions; `ctx` handles captured
evidence and the optional verified edit workflow. Host hooks are optional for
direct `ctx` commands, and their capabilities differ by integration.

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
| Hermes, OMP, OpenCode | Native plugin argument rewrite | Text-output replacement; contract-tested |
| DSH | Deny with a bounded replacement command | Native text-output replacement; contract-tested |

Antigravity's published PreToolUse contract cannot modify arguments. Its
PostToolUse contract cannot replace tool output. straitjacket can prevent a
known command flood before execution, but a verbose connector result can still
enter the transcript unchanged.

See [Host capabilities](HOST-CAPABILITIES.md) for the full matrix.

## Configure the repository

`ctx setup` configures the sidecar's hooks and MCP integration with the selected
coding harness. To opt into [ACP orchestration](ACP.md), add `--acp` and an
explicit `--acp-model` for one host. Existing CLI worker transports remain
available for hosts without ACP configuration.

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

It preserves unrelated user-owned settings. Codex TOML and OpenCode JSON/JSONC
are edited through parsed source spans: comments, other integrations and
unrelated formatting stay in place. Changes to a pre-existing configuration
are previewed before the first write; rerun with `--apply` to accept safe
additions. Explicit conflicts still refuse, including `hooks = false` or a
different `ctx-harness` server. A matching readiness receipt can skip repeated
writes; use `ctx setup --repair` to force all checks and repair owned entries. Use `ctx setup --all` to
prepare all registered vendor integrations; `antigravity-sdk` remains an
explicit opt-in. Hermes needs its installed CLI to activate profile settings;
DSH needs the generated `--patch` at launch. See
[the additional agent integrations](AGENT-INTEGRATIONS.md).

Setup reports host-configuration writes. The persistent host configuration is:

| Scope | Files or entries |
|---|---|
| Workspace | `ctx.toml`, `.ctxignore` |
| Antigravity | `.agents/plugins/ctx-harness/`; a ctx `statusLine` in `~/.gemini/antigravity-cli/settings.json` only when no status line already exists |
| Claude Code | ctx hook entries and, when absent, a ctx `statusLine` in `.claude/settings.json`; `.claude/agents/ctx-explorer.md` only when absent; a managed block in `CLAUDE.md` |
| Codex | ctx entries in `.codex/config.toml` and `.codex/hooks.json`, a managed block in `AGENTS.md` |
| Hermes | Active profile's MCP entry and enabled `straitjacket` plugin; `.ctx/hosts/hermes.json` records the recipe |
| OMP | `mcpServers.ctx-harness` in `.omp/mcp.json` |
| OpenCode | `mcp.ctx-harness` in the existing `opencode.json` or `opencode.jsonc`, plus `.opencode/plugins/straitjacket.js`; two project configs refuse |
| DSH | `.ctx/hosts/dsh.cordis.patch.yml`, loaded with `--patch` |

Setup also writes a content-free readiness receipt to
`.ctx-session-reads/setup.json`; this is not host configuration.

Preview the exact changes to your existing configuration without writing:

```bash
ctx setup --host codex --host opencode --preview
ctx setup --host codex --host opencode --apply
ctx doctor
```

`--preview` requires explicit Codex/OpenCode hosts. It creates no files or
readiness receipt. The default `ctx setup` also stops for review before editing
a pre-existing configuration. Fresh installs retain the existing setup flow.
`ctx wrap codex --print-config` still renders a generic configuration; use
`--preview` for a diff against your actual files.

A conflict prints a unified diff naming the exact key and proposed value.
`--apply` does not override conflicting user settings. Reconcile those entries
using the diff, then rerun preview. Duplicate keys, invalid syntax, symlinks
and ambiguous config files refuse with a diagnostic. A parse error must be
fixed before a trustworthy structural diff can be generated.

Setup records individual contributions in `.ctx/host-config.json`; the
readiness receipt remains content-free. Repair updates a previously owned
value only when its value and source still match the installed contribution,
and restores missing contributions. Comments added inside an owned value are
user edits; repair/removal refuses to overwrite them and shows the exact diff. A banner comment alone never authorizes replacing a
whole file. Existing matching entries are not claimed by setup.

To remove contributions introduced by this flow:

```bash
ctx setup --host codex --host opencode --remove
ctx setup --host codex --host opencode --remove --apply
```

Removal defaults to a preview. An untouched configuration is restored exactly;
if unrelated settings changed afterward, removal deletes only owned entries
and retains those settings and comments. Modified owned values refuse removal.
Pre-existing unowned entries, workspace policy, and captured artifacts remain.
For older installs without ownership records, use the
[manual removal steps](TROUBLESHOOTING.md#how-do-i-turn-the-harness-off-or-uninstall-it).

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
