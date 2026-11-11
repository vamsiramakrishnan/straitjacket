# ADR 003: Route before execution; do not repair after execution

## Status

Accepted.

> Updated 2026-11-11: see [the dated update at the end](#update-2026-11-11). It supersedes the statements that `PostToolUse` is not used and that `PreToolUse` only denies on Antigravity, and the mention of a separate strict policy.

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
