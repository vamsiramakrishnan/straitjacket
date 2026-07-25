# ADR 005 — Antigravity's hook contract, as published

**Status:** accepted · **Date:** 2026-07-25 ·
**Source:** <https://antigravity.google/docs/hooks> (read 2026-07-25)
**Supersedes:** the assumed input-substitution contract previously encoded in
`ctx.hook._to_antigravity_schema`

## Context

The harness contains floods with two gates: a **birth gate** (PreToolUse —
contain the command before it runs) and an **output gate** (PostToolUse —
replace an oversized result with a digest before the model sees it). Both were
implemented against Claude Code's published contract and then mirrored onto
Antigravity by assumption. The docstring said so plainly:

> Assumed Antigravity input-substitution contract (mirrors the decision schema;
> not yet published upstream)

That assumption was wrong in both directions, and the shipped plugin also
registered an event Antigravity does not have.

## The published contract

Events: `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`.
There is **no `SessionStart`**.

| event | output schema |
|---|---|
| `PreToolUse` | `{"decision": "allow"\|"deny"\|"ask"\|"force_ask", "reason"?: str, "permissionOverrides"?: [str]}` |
| `PostToolUse` | `{}` |
| `PreInvocation` | `{"injectSteps": [{"toolCall"\|"userMessage"\|"ephemeralMessage"}]}` |
| `PostInvocation` | `{"injectSteps"?: [...], "terminationBehavior"?: "force_continue"\|"terminate"}` |
| `Stop` | `{"decision": "continue", "reason"?: str}` |

`hooks.json` lives in a customization directory (`.agents/` or
`~/.gemini/config/`), groups handlers under a user-chosen name, and supports a
regex `matcher` on `PreToolUse` / `PostToolUse` only.

Two consequences matter:

1. **PreToolUse cannot modify tool arguments.** There is no `updatedInput` and
   no equivalent. A hook can only gate the call.
2. **PostToolUse cannot alter anything.** Its only legal output is `{}` — not a
   replacement, not a nudge.

## Decision

- **Birth gate: deny, and name the contained command.** A decision carrying a
  `rewrite` degrades on this host to `{"decision": "deny", "reason": "… Re-run
  it as: ctx run -- <cmd>"}`. Containment is preserved — the flood never
  happens — at the cost of one extra turn, because the agent re-issues the
  command rather than having it silently substituted. The `rewrite` payload now
  carries the rewritten `command` string so the reason can name it.
- **Output gate: emit `{}` and stay observational.** The hook still captures
  the bytes into the artifact store, so `ctx get` resolves them later; it makes
  no attempt to change the transcript. The previous
  `{"decision": "allow", "reason": nudge}` was not a legal PostToolUse output.
- **Pre-flight advisory moves to `PreInvocation`.** The capability-surface
  advisory is emitted as `{"injectSteps": [{"ephemeralMessage": …}]}`.
  *Ephemeral* is deliberate: `PreInvocation` fires before every model call, not
  once per session, so a persistent message would re-enter context on each
  invocation — exactly the accumulation the project exists to prevent.
- **The capability is declared, not implied.** `HostSpec` gains
  `input_substitution` alongside `output_substitution`; Antigravity sets both
  `False`, Claude Code and Codex both `True`.

## Consequences

Antigravity is now the one supported host with **no output-side safety net**.
If the birth gate misses a command, nothing downstream can shrink the result —
there is no second line of defence. That raises the stakes on PreToolUse
coverage for this host specifically, and it is why the bounded `ctx` MCP tool
matters more there than elsewhere: MCP results arrive already capped, which is
the only containment path that does not depend on the birth gate.

The deny-based birth gate is also visible to the user in a way the Claude Code
path is not: the agent is refused and retries. That is a real UX cost of the
published contract, not a defect in the classifier, and it should not be
"fixed" by going back to emitting fields the host does not accept.

## Re-checking this

The contract is upstream documentation and can change. `spec/REFERENCES.md`
carries the URL; if Antigravity later publishes an input- or output-substitution
field, the mapper in `ctx.hook` is the single place that changes, and
`HostSpec.input_substitution` / `output_substitution` are the flags that record
it.
