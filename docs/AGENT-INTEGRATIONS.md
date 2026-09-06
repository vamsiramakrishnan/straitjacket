# Keep your agent; add Straitjacket

**Straitjacket is a context and evidence sidecar, not a replacement coding
harness.** The harness owns the model and tool loop. The sidecar adds bounded
capture, retrieval, and optional edit verification.

<p>
  <a href="https://code.claude.com/"><img src="../assets/agents/claude.svg" width="36" height="36" alt="Claude Code" title="Claude Code"></a>
  <a href="https://developers.openai.com/codex/"><img src="../assets/agents/codex.svg" width="36" height="36" alt="Codex" title="Codex"></a>
  <a href="https://antigravity.google/"><img src="../assets/agents/antigravity.png" width="36" height="36" alt="Antigravity" title="Antigravity"></a>
  <a href="https://github.com/NousResearch/hermes-agent"><img src="../assets/agents/hermes.svg" width="36" height="36" alt="Hermes" title="Hermes"></a>
  <a href="https://github.com/can1357/oh-my-pi"><img src="../assets/agents/omp.svg" width="36" height="36" alt="Oh My Pi" title="Oh My Pi"></a>
  <a href="https://opencode.ai/"><img src="../assets/agents/opencode.svg" width="36" height="36" alt="OpenCode" title="OpenCode"></a>
  <a href="https://github.com/deepseek-ai/deepseek-harness"><img src="../assets/agents/dsh.svg" width="36" height="36" alt="DeepSeek Harness" title="DeepSeek Harness"></a>
</p>

Continue using Claude Code, Codex, Antigravity, Hermes, Oh My Pi, OpenCode, or
DeepSeek Harness. Straitjacket adds evidence capture, bounded retrieval, and an
optional verified edit workflow. Your coding agent retains its conversation,
models, authentication, native tools, and permission prompts.

This guide covers the four new MCP integrations. For Claude Code, Codex, and
Antigravity setup, see [Getting started](GETTING-STARTED.md). The
[capability matrix](HOST-CAPABILITIES.md) distinguishes explicit tools from
automatic interception.

## What these integrations provide

The agent receives one MCP tool, `ctx`, for bounded repository navigation and
artifact retrieval. It can search stored output, retrieve cited regions, and
inspect symbols. Command execution stays in the agent's native terminal tool:

```bash
ctx run -- pytest -q
ctx get run:<id>#stdout --lines 120:180
```

Replace `<id>` with the handle printed by the run. Ask the agent to use
`ctx run` for noisy commands and the `ctx` MCP tool for follow-up evidence.
Use the [edit workflow](EDIT-LOOP.md) when you want edits bound to observed
source and verification receipts.

These integrations do **not** intercept arbitrary native reads, commands, or
edits. They install no PreToolUse/PostToolUse hooks. `ctx orchestrate` does not
yet launch these four hosts; wrapper launch and orchestration are separate
capabilities. No model or price is assumed for a user's multi-provider agent.

## Install and choose one host

`ctx setup` installs the hook or MCP wiring described here. It does not install
an ACP client or enable ACP orchestration. The ACP transport is currently
proposed in [ADR 006](../spec/adr/006-acp-orchestration-transport.md).

Install Straitjacket and the chosen agent separately. This setup does not
install an agent or configure provider credentials. The commands below require
a Straitjacket build containing these integrations; they are not a claim that
the current PyPI release already includes them.

From the repository where the agent will work:

```bash
ctx wrap detect
ctx setup --host omp
ctx doctor
```

Use `hermes`, `omp`, `opencode`, or `dsh` for `--host`. The aliases
`open-hermes` and `oh-my-pi` also work. Here, Open Hermes means
[Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent),
not the OpenHermes model family.

| Agent | Configuration | Start after setup |
|---|---|---|
| Hermes | Active profile's `mcp_servers.ctx-harness`, written through Hermes' config CLI; local recipe in `.ctx/hosts/hermes.json` | `hermes chat` |
| Oh My Pi | `.omp/mcp.json`, `mcpServers.ctx-harness` | `omp` |
| OpenCode | `opencode.json`, `mcp.ctx-harness` | `opencode` |
| DeepSeek Harness | `.ctx/hosts/dsh.cordis.patch.yml`, an MCP-client overlay | `ctx wrap dsh -- --profile web` |

OMP and OpenCode setup merges unrelated JSON settings and MCP servers. An
existing conflicting `ctx-harness` entry, malformed file, or symlink refuses
the write. If `opencode.jsonc` exists, setup prints an actionable refusal;
merge the rendered `mcp` entry into that file yourself. Straitjacket does not
rewrite comments or create a competing JSON file.

Preview a host's native configuration without writing:

```bash
ctx wrap opencode --print-config
```

## Hermes profiles

Install a Hermes version with `config get <key> --json` and `config set`.
Setup uses these native commands to inspect and add only the `ctx-harness`
entry. Existing models and other MCP servers remain configured. Hermes owns
the YAML serialization; Straitjacket does not parse or rewrite that YAML.

The entry belongs to Hermes' active profile and applies across its sessions.
Its workspace argument uses Hermes' `${workspaceFolder}` interpolation. To
target a profile, select it using Hermes' profile controls before setup, or set
`HERMES_HOME` to that profile directory for both setup and launch. Rerun setup
when switching profiles. `ctx doctor` checks the currently active profile.
The wrapper rejects launch-only `--profile` / `-p` flags so it cannot configure
one profile and then launch another.

If Hermes is absent, setup can prepare the local recipe, but validation reports
that the integration is not active. Install Hermes and rerun setup. Config
command errors and administrator restrictions remain failures; setup does not
bypass them.

## DSH profiles and overlays

DSH is in developer preview. Straitjacket renders a separate Cordis patch that
inserts `@deepseek-ai/dsh-mcp-client`; DSH ships this dependency. The patch is
JSON, which is valid YAML, and contains no executable YAML tags.

```bash
ctx wrap dsh -- --profile web
ctx wrap dsh -- --profile headless "Inspect this repository using the ctx MCP tool"
```

The wrapper prepends `--patch <absolute-path>` and preserves your remaining
arguments and DSH's exit status. You can instead use the complete `dsh` command
printed by setup. A plain `dsh web` invocation does not load this overlay.
`ctx doctor` verifies the patch contents, not a running DSH connection.

## Launch through a wrapper

OMP, OpenCode, and Hermes can also be launched after setup in one command:

```bash
ctx wrap omp -- --model <your-model>
ctx wrap opencode -- run "Inspect this repository using ctx"
ctx wrap hermes -- chat
```

Replace `<your-model>` with an identifier supported by your OMP installation.
Arguments after `--` go to the agent without shell expansion. Configuration
persists after exit. These are not ephemeral wrappers. Proxy, gateway, and
orchestration wrapper options are unsupported for these four hosts and return
an error.

## Verify and remove

`ctx doctor` validates configured entries. To check the running integration,
start the host, locate its `ctx-harness` MCP server, and ask its `ctx` tool for
`{"op":"repo","workspace":"/absolute/path/to/project"}`. Confirm that the
result describes your repository. The explicit workspace is useful when a
host's terminal runs remotely or in a container: the MCP subprocess must have
access to the same checkout. Setup targets the machine running `ctx`.

To remove, stop sessions using the integration, then:

- OMP: remove only `mcpServers.ctx-harness` from `.omp/mcp.json`.
- OpenCode: remove only `mcp.ctx-harness` from the JSON or JSONC file you used.
- Hermes: run `hermes config unset mcp_servers.ctx-harness` in the same profile
  and delete `.ctx/hosts/hermes.json`.
- DSH: stop passing the patch and delete `.ctx/hosts/dsh.cordis.patch.yml`.

Keep unrelated settings and captured artifacts unless you also intend to
remove them. Deleting a server entry leaves its surrounding config file;
`ctx doctor` will report it missing until you remove that unused file or
reinstall the integration.

## Compatibility evidence

The implementation follows these upstream contracts, reviewed on 2026-09-06:

- [Hermes MCP configuration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md)
  and [config CLI](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md).
- [OMP MCP configuration](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md).
- [OpenCode local MCP servers](https://opencode.ai/docs/mcp-servers/).
- [DSH CLI profiles](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md)
  and [MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md).

Tests cover native config shapes, preservation and refusal, simulated host
launches and Hermes config commands, and real Straitjacket MCP subprocess
round trips. Live agent/model sessions remain unverified. Successful rendering
alone does not establish host-version compatibility or better task outcomes.
