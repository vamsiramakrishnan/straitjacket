# ADR 002: Store payloads outside the repository

## Status

Accepted for the plugin/hardened mode.

## Decision

Committed repository state consists of `ctx.toml`, `.ctxignore`, and Antigravity integration files. Payloads and indexes live in an OS user-state directory or broker-owned store.

## Rationale

Storing blobs inside `.ctx/` makes them easy for an unrestricted shell to read and easy to accidentally commit, delete, or couple to plugin lifecycle. A workspace-scoped capability handle is safer than exposing a path.

## Caveat

This is not a security boundary when Antigravity is run with unrestricted access to the entire user home. The runtime and documentation must say so plainly.
