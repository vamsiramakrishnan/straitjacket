# ADR 003: Route before execution; do not repair after execution

## Status

Accepted.

> Updated 2026-11-11 (twice): see [the dated updates at the end](#update-2026-11-11). The second one narrows the decision itself: on Claude Code and Codex ctx no longer routes shell commands through `ctx` before execution by default, and bounds the output afterwards. It supersedes the statements that `PostToolUse` is not used and that `PreToolUse` only denies on Antigravity, and the mention of a separate strict policy.

## Decision

The Antigravity plugin uses `PreToolUse` to deny or escalate potentially unbounded native commands and reads. The remediation is to invoke `ctx run` or bounded repo retrieval. `PostToolUse` is not used as an output-transform mechanism.

## Rationale

Once a native tool has returned a large payload, the transcript has already been polluted. Capturing it afterward may preserve the bytes but cannot recover the lost prefix/cache property.

## Availability

The context guard defaults to fail-open on internal hook failure and always emits valid JSON. A separate strict policy may fail closed.

## Update 2026-11-11

Two statements above are out of date. The Antigravity plugin registers
`PostToolUse`, but only to record the call: on `agy` 1.5.5 it receives no tool
result and may only answer `{}`, so it still is not an output-transform mechanism.
And `PreToolUse` no longer only denies: for `run_command` and `read_file` it rewrites
the arguments in place (see [ADR 005](005-antigravity-hook-contract.md)).

There is no "separate strict policy". `[guard] mode = "strict"` is accepted but has no
code path of its own and behaves as `guarded`; the fail-closed choice is
`[guard] internal_error = "deny"`.

## Update 2026-11-11 (routing and approvals)

The decision above, "route before execution; do not repair after execution", no longer
describes the default on Claude Code and Codex. Routing a shell command through `ctx`
required rewriting it, and a rewrite could change what the command returned (a quoted
glob, a recursive search answered by a symbol lookup, a whole-suite test run answered
by a lookup of earlier failures). On Claude Code it also had to be answered with
`allow`, which overrides the user's deny and ask rules. By default (`[guard] steering =
"auto"`) a shell command on those hosts therefore reaches the host exactly as written,
the host decides about it, and the output is bounded afterwards by the PostToolUse
gate. This is the "repair after execution" the decision argued against; it is chosen
because it cannot change the command's answer or the user's approvals. Routing before execution remains available as
`steering = "rewrite"`, and is the default only where output cannot be replaced
(Antigravity). Where a rewrite happens, the original command is passed verbatim as
`ctx run --passthrough --shell -- '<original>'`. See
[Configuration](../../docs/CONFIGURATION.md#steering-what-happens-to-a-shell-command).
