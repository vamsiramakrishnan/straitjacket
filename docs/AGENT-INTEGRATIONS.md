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

This guide covers the four new native integrations. For Claude Code, Codex, and
Antigravity setup, see [Getting started](GETTING-STARTED.md). The
[capability matrix](HOST-CAPABILITIES.md) distinguishes explicit tools from
automatic interception.

## What these integrations provide

The agent receives `ctx` for bounded navigation and retrieval, and `ctx_edit`
for anchored patches and structural rewrites. Native plugins connect the same
guard and edit-outcome ledger used by the existing integrations. Command
execution stays in the agent's native terminal tool: use `ctx run -- pytest -q`
to capture a noisy run and `ctx` to retrieve follow-up evidence.

Hermes, OMP, and OpenCode can rewrite recognized command inputs and replace
oversized text results. DSH gates calls and bounds text but cannot change sealed
arguments; it returns the bounded command for an explicit retry. Native edits
are observed in the edit ledger, not silently converted into verified
transactions. Use `ctx_edit` when you need those guarantees.

Output replacement covers text, not image/audio blocks or metadata. OMP direct
eval bridges bypass tool events, and error results can be rethrown unchanged.
DSH preserves downstream canonical-value transformations; its content gate is
not a confidentiality boundary for programmatic values. See the
[capability matrix](HOST-CAPABILITIES.md) for the complete scope.

## Install and choose one host

`ctx setup` installs native hooks and MCP wiring. To configure ACP through the
same setup command, select one host and model:

```sh
ctx setup --host opencode --acp --acp-model provider/model-id
```

Replace the model placeholder with the exact id your agent advertises.
[ACP setup](ACP.md) covers all seven agents, adapter commands, and permissions.
ACP workers receive MCP tools in their actual worktree; interactive setup
persists in the agent's native configuration.

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
| Hermes | Active profile's MCP entry and enabled `plugins/straitjacket/`; local recipe in `.ctx/hosts/hermes.json` | `hermes chat` |
| Oh My Pi | `.omp/mcp.json` and `.omp/hooks/pre/straitjacket.js` | `omp` |
| OpenCode | `opencode.json` and `.opencode/plugins/straitjacket.js` | `opencode` |
| DeepSeek Harness | `.ctx/hosts/dsh.cordis.patch.yml`, loading MCP and `.ctx/hosts/straitjacket-dsh.mjs` | `ctx wrap dsh -- --profile web` |

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
Setup adds the `ctx-harness` MCP entry, locates the active profile with
`hermes config path`, installs its `straitjacket` plugin, and enables it with
`hermes plugins enable straitjacket`. The plugin runs only where `ctx.toml`
is present above the working directory. Existing models and other MCP servers remain configured. Hermes owns
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
inserts `@deepseek-ai/dsh-mcp-client` and the managed native hook module. DSH
ships the MCP dependency. The patch is JSON, which is valid YAML, and loads
the JavaScript plugin through Cordis' normal plugin loader.

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

## Verified patches and rewrites

First retrieve an anchored source span with `ctx`. Call `ctx_edit` with
`op: "replace"`, its `ref` and `span`, and the replacement text. The default is
a preview: inspect its patch, then use `op: "apply"` with the returned `planRef`.
Stale or ambiguous source is refused. Structural rewrites use `op: "rewrite"`
with a pattern, replacement, language, and optional glob; `op: "rewrite_apply"`
requires that preview's `receiptRef`. Structural rewrites require ast-grep
and refuse incomplete previews. Applying proves which bytes changed;
[verification](EDIT-LOOP.md) checks whether they are correct.

## Verify and remove

`ctx doctor` validates configured entries. To check the running integration,
start the host, locate its `ctx-harness` MCP server, and ask its `ctx` tool for
`{"op":"repo","workspace":"/absolute/path/to/project"}`. Confirm that the
result describes your repository. The explicit workspace is useful when a
host's terminal runs remotely or in a container: the MCP subprocess must have
access to the same checkout. Setup targets the machine running `ctx`.

To remove, stop sessions using the integration, then:

- OMP: remove `mcpServers.ctx-harness` from `.omp/mcp.json` and the managed
  `.omp/hooks/pre/straitjacket.js` plugin.
- OpenCode: remove `mcp.ctx-harness` from the JSON or JSONC file you used and
  `.opencode/plugins/straitjacket.js`.
- Hermes: run `hermes config unset mcp_servers.ctx-harness` in the same profile
  and `hermes plugins disable straitjacket`; remove the managed profile plugin
  and `.ctx/hosts/hermes.json` / `.ctx/hosts/hermes-plugin/` recipes.
- DSH: stop passing the patch; remove `.ctx/hosts/dsh.cordis.patch.yml` and
  `.ctx/hosts/straitjacket-dsh.mjs`.

Remove a host from `.ctx/acp.json` to restore its original orchestration
transport. Stop existing sessions before removing their active integrations.

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

Tests cover configuration preservation/refusal, executable Python/JavaScript
callbacks, real MCP edit transactions, and stdio ACP protocol exchanges. Live agent/model sessions remain unverified. Successful rendering
alone does not establish host-version compatibility or better task outcomes.

Native hook contracts:

- [Hermes hooks](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md).
- [OMP extension wrapper](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/extensibility/extensions/wrapper.ts).
- [OpenCode plugins](https://opencode.ai/docs/plugins/).
- [DSH interception contracts](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/implemented/feature/2026-06-30-interception-extension-points.md).
