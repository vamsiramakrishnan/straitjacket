# ADR 006: Add ACP as an orchestration transport

Status: proposed; no ACP client is implemented by this change.

## Problem

`ctx orchestrate` currently launches agent processes with host-specific flags
and parses their output. Each new host needs another launch, progress, usage,
and shutdown contract. A successful CLI launch does not establish session
resumption, permission handling, or cancellation behavior.

The Hermes, OMP, OpenCode, and DSH integrations add explicit MCP tools and
terminal workflows. They intentionally do not claim an orchestration adapter.
Adding more one-shot CLI branches would increase the maintenance cost before
the common session contract has been isolated.

## Proposed approach

Make Straitjacket an Agent Client Protocol client for worker agents that expose
a verified ACP endpoint. Keep routing, model eligibility, budgets, task ledgers,
worktree isolation, edit receipts, and verification in Straitjacket. Introduce a
worker transport interface below those mechanisms, with CLI and ACP backends.

ACP handles conversations with an agent. MCP exposes Straitjacket's bounded
evidence tool to that agent. The two protocols have different responsibilities:

| Layer | Responsibility |
|---|---|
| Straitjacket orchestrator | Select work, enforce budgets, isolate changes, verify results, persist task state |
| ACP worker transport | Negotiate capabilities, start a session, submit prompts, receive updates, cancel, and handle permission requests |
| MCP evidence server | Bounded navigation and retrieval; no arbitrary command execution |
| Agent | Run its own model and tool loop under its authentication and permissions |

When supported, pass the `ctx` MCP server to ACP `session/new`. This makes the
tool configuration session-scoped and avoids persistent agent configuration for
orchestrated workers. Interactive users can continue launching their agents
normally with the integrations described in
[Agent integrations](../../docs/AGENT-INTEGRATIONS.md).

ACP is not an edit-verification contract or a universal interception hook.
Receiving an agent's tool update does not prove that the client could replace
the tool result before inference. A completed prompt is not a verified code
change. Existing evidence and verification requirements still apply.

## Small implementation steps

1. Extract the current CLI worker interface without changing routing or
   outputs. Define typed progress, completion, cancellation, and usage records.
   Preserve existing receipts as the regression baseline.
2. Add a stdio ACP client behind an opt-in transport setting. Implement
   initialization, session creation, prompting, updates, cancellation, bounded
   stderr capture, and deterministic teardown. Advertise only client
   capabilities actually implemented. Unknown methods receive a protocol
   error; they cannot trigger file or terminal actions.
3. Handle permission requests explicitly. Interactive runs use a configured
   approval handler. Unattended runs cancel requests they cannot authorize;
   never auto-approve or broaden the agent's permissions to finish a task.
4. Pilot DSH's documented `dsh --profile acp` endpoint. Add a second independently
   implemented agent only after verifying its advertised ACP entry point and
   version. Retain CLI backends for unsupported agents.
5. Promote transport selection per host only after live receipts establish
   tool injection, progress, cancellation, permission refusal, and completion.
   Resume/load support and usage accounting are capability-dependent. Missing
   usage remains unknown, not zero, and cannot justify budget claims.

## Acceptance gates

Use a local fake ACP agent for out-of-order JSON-RPC responses, interleaved
updates, malformed frames, bounded buffering, cancellation during permission
requests, timeouts, process exits, and cleanup of pending sessions. Verify that
failed or cancelled sessions never produce successful worker receipts.

Then run the same fixed task against a version-pinned live agent through both
CLI and ACP. Record the agent and model, task outcome, patch, verification
receipt, wall time, reported usage, and transport failures. Confirm that the
agent can call the injected `ctx` tool in the intended worktree. Do not select
ACP merely because both transports return final text.

Keep endpoint launch commands explicit. Do not assume that installing a CLI
means it supports ACP, fetch adapters at runtime without an explicit setup
step, or infer an endpoint from the host's name.

## Sources

- [ACP overview](https://agentclientprotocol.com/protocol/v1/overview)
- [ACP schema, including session creation and MCP servers](https://agentclientprotocol.com/protocol/v1/schema)
- [DSH launcher and ACP profile](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md)

Reviewed 2026-09-06. This is an incremental migration proposal, not a claim of
ACP conformance or live compatibility with all registered hosts.
