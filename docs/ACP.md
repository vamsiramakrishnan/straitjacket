# ACP orchestration

Straitjacket can launch workers through Agent Client Protocol v1 over stdio.
The coding agent still owns its model, authentication, and tool loop. Straitjacket
owns task routing, evidence, worktree isolation, and verification.

## Setup

Install and authenticate your agent's ACP endpoint, then configure one host:

```sh
ctx setup --host opencode --acp --acp-model 'provider/model-id'
ctx orchestrate 'Describe the task'
```

Use the **exact model id advertised by the agent**, replacing the placeholder
above. Setup writes `.ctx/acp.json`; ordinary `ctx setup` continues configuring
interactive hooks/MCP. Each ACP worker receives the `ctx` MCP server through
`session/new`, with its actual workspace or isolated worktree as the root.

| Host | Default endpoint argv | Endpoint source |
|---|---|---|
| Claude Code | `claude-agent-acp` | [ACP adapter](https://github.com/agentclientprotocol/claude-agent-acp) |
| Codex | `codex-acp` | [ACP adapter](https://github.com/agentclientprotocol/codex-acp) |
| Antigravity | `agy_acp_server.par --uid=` | [Google ACP server registry entry](https://github.com/agentclientprotocol/registry/blob/main/antigravity-acp/agent.json) |
| Hermes | `hermes acp` | [Hermes entry point](https://github.com/NousResearch/hermes-agent/blob/main/acp_adapter/entry.py) |
| OMP | `omp acp` | [OMP command](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/commands/acp.ts) |
| OpenCode | `opencode acp` | [OpenCode ACP docs](https://opencode.ai/docs/acp/) |
| DSH | `dsh --profile acp` | [DSH launcher](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md) |

Use `--acp-command '["/absolute/path/to/adapter", "argument"]'` when the installed
binary has another name or needs extra flags. Commands are argv arrays, never
shell fragments. Setup checks that the executable exists; it does not download
adapters, log into providers, or establish a live session. `ctx doctor` reports
configuration checks separately from live compatibility.

`--acp-tier economy|standard|frontier` declares the model's routing tier
(default `standard`). The configured model is the only model offered to the
router for that endpoint. Before sending a task, Straitjacket checks the agent's
model catalog and selects that exact id. An absent model is an error. A host
without ACP configuration keeps its existing CLI transport.

## Permissions and limits

ACP permission requests default to refusal. An unattended worker that encounters
an unresolved request fails rather than reporting completion. For a workspace
where you intend to authorize each request automatically, explicitly pass
`--acp-permissions allow_once` at setup. This chooses only the agent's offered
`allow_once` option; it does not change persistent permissions or choose
`allow_always`.

The transport implements initialization, session creation, model selection,
prompt streaming, permission responses, cancellation, and process-group cleanup.
It advertises no client filesystem or terminal capabilities. Unknown client
requests receive a protocol error. ACP tool notifications are progress events;
they are **not** interception hooks. Native hooks and explicit `ctx run`,
`ctx edit`, and `ctx rewrite` remain responsible for those operations.

Frames and final text are limited to 2 MiB each; stderr capture is limited to
64 KiB. Exceeding a limit, timing out, receiving an invalid frame, or ending with
a stop reason other than `end_turn` fails the worker. Missing usage remains
unknown. Session reload/resume, client-owned terminals/files, and live provider
usage accounting are not implemented in this transport.

## Validation

`tests/test_acp.py` exercises real subprocess exchanges against a deterministic
ACP fixture, including every configured host. These are protocol and wiring
tests, not live runs of all seven agents. Validate your installed adapter and
model with a small task before relying on unattended work.

See [agent integrations](AGENT-INTEGRATIONS.md) for interactive setup and
[host capabilities](HOST-CAPABILITIES.md) for interception limits.
