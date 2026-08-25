# Getting started

[Documentation](README.md) · [How it works](HOW-IT-WORKS.md) · [Troubleshooting](TROUBLESHOOTING.md)

This guide installs straitjacket, configures a coding-agent host, and runs the core capture and retrieval loop.

## Requirements

- Python 3.11 or newer.
- A local repository.
- Antigravity, Claude Code, or Codex for host integration.

Host integration is optional. Every `ctx` command also works from a terminal.

`rg`, ctags, tree-sitter, Jedi, SCIP, and Semgrep can improve repository analysis. Capture and retrieval do not depend on them.

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

## Configure the repository

```bash
cd your-repository
ctx setup
ctx doctor
```

`ctx setup`:

1. creates or updates `ctx.toml` and `.ctxignore`;
2. detects supported agent CLIs;
3. merges host configuration;
4. runs the same checks as `ctx doctor`;
5. records a content-free fingerprint for fast verified reruns.

It does not replace an existing host configuration. An unchanged rerun is a verified no-op. Use `ctx setup --repair` to force verification and repair. Use `ctx setup --all` to prepare every supported host.

Configure one host only:

```bash
ctx wrap antigravity
ctx wrap claude
ctx wrap codex
```

Preview the exact host configuration without writing it:

```bash
ctx wrap codex --print-config
```

## Know the host boundary

| Host | Birth-time containment | Oversized result substitution |
|---|---|---|
| Claude Code | Transparent rewrite | Yes |
| Codex | Transparent rewrite | Yes |
| Antigravity | Deny with a bounded replacement command | No |

Antigravity's published PreToolUse contract cannot modify arguments. Its PostToolUse contract cannot replace tool output. straitjacket still prevents known command floods before execution, but a verbose connector result can enter the transcript unchanged. Use bounded `ctx` retrieval operations for those paths.

See [Host capabilities](HOST-CAPABILITIES.md) for the full matrix.

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
- workspace-relative path confinement;
- traversal and symlink-escape rejection;
- timeouts and process-group handling;
- secret-aware controls.

It is not a complete process sandbox. Commands run with the invoking user's permissions.

## Next

- [CLI guide](CLI.md)
- [Configuration](CONFIGURATION.md)
- [Core concepts](CONCEPTS.md)
- [Architecture](ARCHITECTURE.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Evaluation receipts](../evals/)
