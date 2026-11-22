# Run a coding harness as an ACP worker

Straitjacket starts each worker as a subprocess and speaks Agent Client
Protocol v1 over stdio. The adapter opens a session in the assigned checkout,
selects a model and runs the task. The harness supplies authentication and
model access. Straitjacket supplies the task, bounded evidence tools, peer
messages and cancellation.

## Configure two workers

Install and authenticate the adapters you intend to use. From the project
root, set `CODEX_MODEL` and `OPENCODE_MODEL` to exact IDs advertised by your
respective adapters. Run setup once per host:

```sh
ctx setup --host codex --acp --acp-model "${CODEX_MODEL:?set the Codex adapter model ID}" --acp-tier frontier
ctx setup --host opencode --acp --acp-model "${OPENCODE_MODEL:?set the OpenCode adapter model ID}" --acp-tier standard
ctx doctor
ctx orchestrate 'Fix a typo in README.md' --dry-run
```

The tiers above illustrate a pairing. Choose tiers that match your selected
models. Each setup records one host in `.ctx/acp.json` and configures its
native integration. The preview shows the selected worker for each node. A
second configured endpoint gives the router another choice. See
[Routing](ROUTING.md) for assignment rules and
[Harness collaboration](HARNESS-COLLABORATION.md) for handoffs.

| Host | Default endpoint argv | Endpoint source |
|---|---|---|
| Claude Code | `claude-agent-acp` | [ACP adapter](https://github.com/agentclientprotocol/claude-agent-acp) |
| Codex | `codex-acp` | [ACP adapter](https://github.com/agentclientprotocol/codex-acp) |
| Antigravity | `agy_acp_server.par --uid=` | [Google ACP server registry entry](https://github.com/agentclientprotocol/registry/blob/main/antigravity-acp/agent.json) |
| Hermes | `hermes acp` | [Hermes entry point](https://github.com/NousResearch/hermes-agent/blob/main/acp_adapter/entry.py) |
| OMP | `omp acp` | [OMP command](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/commands/acp.ts) |
| OpenCode | `opencode acp` | [OpenCode ACP docs](https://opencode.ai/docs/acp/) |
| DSH | `dsh --profile acp` | [DSH launcher](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md) |

Pass `--acp-command '["/absolute/path/to/adapter", "argument"]'` to
override the default argv. Setup checks for the executable. `ctx doctor`
checks the saved configuration. The live session checks whether the account
can select the exact model **before it sends the task**. An unavailable ID
fails the attempt. For Gemini, use the [model selection
guide](NATIVE-AGY-SESSIONS.md#choose-a-gemini-model).

## What a worker receives

| Step | Contract |
|---|---|
| Open | `session/new` receives the actual workspace or isolated worktree and a session-scoped MCP server exposing `ctx` and `ctx_edit`. |
| Select | Straitjacket checks the configured model against the session's model options and switches to it when needed. |
| Delegate | `session/prompt` carries the node's task, its bounded upstream handoff and any queued advisory. The worker can resolve evidence addresses and submit verified edits through MCP. |
| Finish | A successful attempt ends with `end_turn` and no unresolved permission requests. The orchestrator saves the checkpoint for downstream nodes. Each attempt opens a fresh session. |

ACP permission requests default to refusal. In a workspace where you intend
to grant each request automatically, configure `--acp-permissions allow_once`.
This chooses the adapter's single-use permission when offered. Workspace and
OS access still depend on how the agent runs. Straitjacket advertises no
client-owned filesystem or terminal capabilities over ACP.

Native hooks remain specific to each harness. ACP tool notifications convey
progress. For a shared verified edit contract, workers can use `ctx_edit`.
In an isolated worktree, Straitjacket removes its temporary host wiring before
it captures the patch. See [host capabilities](HOST-CAPABILITIES.md) for native
coverage and [the transport decision](../spec/adr/006-acp-orchestration-transport.md)
for adapter-specific wiring.

## Send a finding or stop a worker

From the same workspace, pass an address the recipient can resolve:

```sh
ctx relay signal codex repo:README.md --origin opencode --note 'Check the installation example'
ctx relay signal codex repo:README.md --origin opencode --interrupt --note 'The target file changed'
```

The first command queues an advisory for the next Codex prompt. The second
requests cancellation of a running Codex ACP turn. Orchestrated workers use
the host name as their address. A worker can also use the `relay_watch`,
`relay_publish` and `relay_pending` operations on its injected `ctx` tool.
See [Relay](RELAY.md) for delivery receipts and interactive host behavior.

## Operation and verification

Frames and final text have a 2 MiB limit each. Stderr capture has a 64 KiB
limit. Invalid frames, timeouts, unresolved permission requests and unexpected
stop reasons fail the attempt. ACP currently returns no provider billing
record. Resuming the [task ledger](TASK-LEDGER.md) restores the workflow and
checkpoints. It creates a new agent session for unfinished work.

[Protocol tests](../tests/test_acp.py) exercise real subprocesses with a
deterministic adapter for all seven host configurations. They check model
selection, permission decisions, MCP wiring, relay delivery and cancellation.
Compatibility with a particular installed adapter and account requires a live
session with that endpoint.
