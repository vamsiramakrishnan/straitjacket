# ADR 003: Route before execution; do not repair after execution

## Status

Accepted.

## Decision

The Antigravity plugin uses `PreToolUse` to deny or escalate potentially unbounded native commands and reads. The remediation is to invoke `ctx run` or bounded repo retrieval. `PostToolUse` is not used as an output-transform mechanism.

## Rationale

Once a native tool has returned a large payload, the transcript has already been polluted. Capturing it afterward may preserve the bytes but cannot recover the lost prefix/cache property.

## Availability

The context guard defaults to fail-open on internal hook failure and always emits valid JSON. A separate strict policy may fail closed.
