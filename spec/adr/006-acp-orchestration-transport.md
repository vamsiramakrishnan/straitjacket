# ADR 006: ACP as an optional orchestration transport

Status: implemented behind explicit per-host setup; live adapter receipts pending.

## Decision

Straitjacket is an ACP v1 client for configured workers. The agent retains its
model, authentication, and tool loop. Straitjacket retains routing, budget
policy, task ledgers, isolated worktrees, edit receipts, and verification.
Existing CLI workers remain available for hosts without ACP configuration.

`ctx setup --host HOST --acp --acp-model MODEL` configures the selected host's
native integration and records an explicit endpoint in `.ctx/acp.json`.
The router sees only that endpoint's configured model and declared tier.
Before prompting, the transport verifies the model against the agent's
advertised catalog and selects it through ACP configuration options or the
legacy model selector. It never infers a model catalog from the CLI's name.

| Layer | Responsibility |
|---|---|
| Orchestrator | Route work, enforce bounds, isolate changes, verify outcomes |
| ACP client | Initialize, create sessions, select models, stream prompts, answer permissions, cancel |
| MCP server | Bounded evidence plus opt-in anchored edits and structural rewrite transactions |
| Native plugins | Gate calls, rewrite inputs, or replace text where the host contract permits |
| Coding agent | Own model requests, authentication, native tools, and agent execution |

Each worker receives the MCP server in `session/new` with its actual worktree.
Temporary worker wiring is removed before patch capture. ACP tool updates do
not prove that the client can intercept a tool result before inference. Native
hook capabilities stay separate from ACP support.

## Implemented bounds

The stdlib-only transport advertises no filesystem or terminal capabilities.
Unknown client requests receive a protocol error. Frames and final text have
2 MiB limits; stderr capture has a 64 KiB limit. Wall and optional idle timeouts
stop the worker, send cancellation where possible, and clean up its process
group. Malformed responses, missing models, unresolved permissions, and stop
reasons other than `end_turn` fail the attempt.

Permissions default to refusal. Users may explicitly configure `allow_once`;
this never chooses `allow_always` or rewrites persistent agent permissions.
Unreported usage stays unknown. Session reload/resume, client-owned file and
terminal operations, and provider-specific usage normalization remain unimplemented.

The common edit path is the opt-in `ctx_edit` MCP tool. It reuses the existing
anchored transaction and structural rewrite engines, requires a fixed workspace,
previews by default, persists full receipts, and refuses stale applications.
Native patch tools retain their own behavior; observing their result does not
turn them into verified edits.

## Compatibility evidence

The ACP fixture runs as a real subprocess and covers initialization, injected
MCP startup, model selection, interleaved notifications/requests, permissions,
malformed and oversized messages, cancellation, timeouts, and completion.
Executable Python/JavaScript plugin tests exercise the new native callback
contracts. MCP tests apply anchored edits and reject stale patch/rewrite plans.

These checks do not establish live compatibility with every installed adapter.
Run a fixed task with a version-pinned agent/model and record tool invocation,
patch, verification, usage, and failures before claiming task-quality or cost
improvements. Do not promote ACP to the default based only on final text.

## Sources

- [ACP protocol](https://agentclientprotocol.com/protocol/v1/overview)
- [ACP session setup](https://agentclientprotocol.com/protocol/v1/session-setup)
- [ACP permissions](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [Setup guide and endpoint sources](../../docs/ACP.md)
- [Native hook contracts and limits](../../docs/AGENT-INTEGRATIONS.md)

Reviewed 2026-09-06.
