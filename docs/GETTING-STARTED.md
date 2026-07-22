# Getting started

This guide takes you from a source checkout to a verified Straitjacket workspace. You will capture one command, retrieve an exact region, and search the stored result without rerunning it.

## Prerequisites

You need:

- Python 3.11 or newer;
- a local project workspace;
- Git for the source installation;
- Antigravity, Claude Code, or Codex only if you want automatic host integration.

The standalone `ctx` commands work without an agent host.

Optional tools such as ripgrep, tree-sitter grammars, SCIP data, Semgrep, and graph libraries improve precision or speed. They are not required for the core capture and retrieval workflow.

## 1. Install from source

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e .
```

Confirm that the CLI is available:

```bash
ctx --help
```

The installed Python package is named `ctx-harness`. The command is `ctx`. The product name is Straitjacket.

## 2. Configure a workspace

Move to the project where the agent will work:

```bash
cd /path/to/your/project
ctx wrap setup
```

`ctx wrap setup` performs two jobs:

1. it writes the workspace files used by the harness, including `ctx.toml` and `.ctxignore` when needed;
2. it configures every supported host for the current workspace.

The operation is idempotent. It merges with existing configuration and does not replace user-owned settings wholesale.

### What setup writes

| Host | Workspace files | Behavior |
|---|---|---|
| Antigravity | `.agents/plugins/ctx-harness/` | Persistent workspace plugin with hooks, MCP configuration, skill, and explorer agent |
| Claude Code | Hook configuration and a bounded `ctx` command card | Persistent workspace setup; an ephemeral wrapper is also available |
| Codex | `.codex/config.toml`, `.codex/hooks.json`, and a managed `AGENTS.md` block | Persistent workspace setup |

Preview the generated configuration for one host without writing it:

```bash
ctx wrap antigravity --print-config
ctx wrap claude --print-config
ctx wrap codex --print-config
```

Configure only one host when required:

```bash
ctx wrap antigravity
ctx wrap codex
```

Run one ephemeral Claude Code session without retaining the wrapper configuration:

```bash
ctx wrap claude -- -p "fix the failing tests"
```

The wrapper injects temporary hook settings for the child process and restores the workspace when the process exits. Existing user configuration remains authoritative.

## 3. Verify the installation

Run the general health check:

```bash
ctx doctor
```

For an Antigravity workspace, include the plugin checks:

```bash
ctx doctor --antigravity
```

Use `--print-config` when a host is not behaving as expected. It shows the exact configuration Straitjacket intends to install and is safe to use in CI or review workflows.

## 4. Capture a command

Run a command through the birth-time capture gate:

```bash
ctx run -- pytest -q
```

The `--` separator ends Straitjacket's options. Everything after it is the child command.

Small output may pass through unchanged. Large output is stored in full and replaced with a bounded digest. A digest includes:

- the command and exit status;
- the detected evidence profile;
- a concise evidence census;
- coverage and omission information;
- retrieval addresses for omitted regions.

Example:

```text
[ctx run:8d8335db6848 profile=pytest/v2]
command: pytest -q
exit: 1
failing tests:
  tests/test_auth.py::test_token_expiry   tests/test_auth.py:42
coverage:
  identities: 1/1
  omitted: 4,098 lines
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```

The `run:` value is an immutable artifact handle. Keep it when you need to inspect, search, compare, pin, or cite the run later.

## 5. Retrieve exact evidence

Use the address suggested by the digest:

```bash
ctx get run:8d8335db6848#stdout --lines 1280:1300
```

A small selection returns exact bytes. A selection that exceeds the retrieval budget returns a bounded zoom view with narrower continuation addresses.

Retrieval cannot recursively flood the transcript.

## 6. Search the stored result

Search the captured artifact without rerunning the command:

```bash
ctx search run:8d8335db6848#stdout "MissingTenantError"
```

Search accepts multiple patterns:

```bash
ctx search run:8d8335db6848#stdout "MissingTenantError" "tenant_id" --context 3
```

Search the live repository by using `repo:` as the reference:

```bash
ctx search repo: "MissingTenantError" --glob "**/*.py" --context 3
```

Repository reads are live and snapshot on retrieval. Captured `run:` and `blob:` artifacts are immutable.

## 7. Choose the right execution shape

Use the least powerful command that expresses the work.

| Work shape | Command |
|---|---|
| One command | `ctx run -- <command>` |
| A shell pipeline | `ctx run --shell '<pipeline>'` |
| Known steps | `ctx seq '<step 1>' '<step 2>'` |
| Computed branching, loops, or aggregation | `ctx eval <script>` |
| Typed evidence composition | `ctx q '<pipeline>'` |
| A repository question with a known intent | `ctx ask "<question>" --intent <intent>` |
| Work that may outlive the turn | `ctx run --bg-after <seconds> -- <command>` |

A useful rule is: batch deterministic fan-out, not uncertainty. Run several known operations locally. Return to the model when the evidence can change the hypothesis or plan.

## 8. Supervise long-running work

```bash
ctx run --bg-after 30 -- ./scripts/integration-test
```

If the command finishes within 30 seconds, it behaves like a normal foreground run. If it continues, Straitjacket returns a `job:` handle while the process remains supervised in the background.

```bash
ctx job <job-id>
ctx job <job-id> --wait
ctx job <job-id> --kill
```

A completed job finalizes into an ordinary `run:` artifact.

## Optional analysis engines

Install all optional Python extras for development and richer repository analysis:

```bash
python -m pip install -e '.[dev,map,fast,code,scip,sem]'
```

The core design requires a documented fallback when an optional engine is absent. A missing accelerator should reduce precision or speed in a disclosed way; it should not break capture.

## Common problems

### `ctx: command not found`

Confirm that the environment used for installation is active and that its scripts directory is on `PATH`.

```bash
python -m pip show ctx-harness
python -m pip install -e /path/to/straitjacket
```

Then open a new shell or reactivate the environment and run `ctx --help`.

### The output was not compressed

Small outputs intentionally pass through. Capture is useful when containment saves context; it should not turn a complete six-line result into a retrieval workflow.

### A host is not intercepting commands

Run:

```bash
ctx wrap <host> --print-config
ctx doctor
```

For Antigravity, also run `ctx doctor --antigravity`. Check that the generated workspace files are visible to the host and that `ctx` resolves from the host process environment.

### An optional engine is missing

The digest or command output should disclose the active fallback. Install the relevant extra only when the richer engine is required for the task.

### A command needs shell syntax

Use `--shell` only when pipes, redirects, variable expansion, or other shell semantics are part of the operation:

```bash
ctx run --shell 'rg -n "TODO" src | sort | head -200'
```

Prefer direct argument execution for a single command.

## Next steps

- Read [How it works](HOW-IT-WORKS.md) for the complete data flow.
- Use the [CLI guide](CLI.md) as the command reference.
- Open [Use cases](USE-CASES.md) for task-specific workflows.
- Read [Core concepts](CONCEPTS.md) before the architecture documents.
- Inspect [`evals/`](../evals/) before relying on a performance claim.

---

[Documentation](README.md) · [How it works](HOW-IT-WORKS.md) · [CLI guide](CLI.md) · [Use cases](USE-CASES.md)
