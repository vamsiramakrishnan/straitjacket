# Prewalk: continue from a verified edit

Prewalk is opt-in (`[orchestrate] prewalk = true`). A frontier model on a
mutation node can hand off to a cheaper installed model after an applied edit
passes an explicit behavioral check. The same worktree continues unchanged.

## Evidence required

The worker uses `ctx edit replace --apply` or `ctx edit apply`, then
`ctx edit verify`, then `ctx edit handoff --verification blob:<proof> --state state.json`.
The launcher sets `CTX_EDIT_ATTEMPT` to the task/node/attempt identity. The
apply receipt records it. Injected launch adapters can use the prompt's
attempt key through the Python API's `attempt_key` argument.

The handoff command emits exactly two lines:

```text
CTX_PREWALK_HANDOFF
CTX_PREWALK_STATE blob:<full-sha256>
```

The orchestrator treats those lines as a request. It loads the addressed state,
apply receipt, verification runs, and diagnostic receipts. It checks the
attempt identity, current file hashes, completed checks, and remaining work.
A bare marker, no-op edit, stale proof, failed check, or invalid worker yield
cannot authorize a cheaper continuation. Ordinary failure handling remains in
force. Syntax-only checks do not qualify as behavioral verification.

An explicit check is still a caller-selected acceptance condition. A passing
trivial command does not prove task correctness. The receipts establish what
ran against which bytes; independent acceptance tests remain necessary.

## Continuation state

The state file has `checklist`, `hypotheses`, `ruledOut`, and `evidence` fields.
Only `checklist` is required. Lists are capped at 12 entries and the entire
state at 16,000 bytes. Each checklist item has `id`, `task`, `validation`, and
`status` (`done` or `pending`). At least one completed and one pending item
must exist. For example:

```json
{
  "checklist": [
    {"id":"fix","task":"Correct token expiry","validation":"Expiry reproducer","status":"done"},
    {"id":"coverage","task":"Check boundary timestamps","validation":"Boundary tests","status":"pending"}
  ],
  "hypotheses": ["Expiry comparison excluded equality"],
  "ruledOut": ["Clock parsing was unchanged"],
  "evidence": ["blob:<apply-receipt>"]
}
```

The full bounded checklist enters the continuation prompt alongside evidence
addresses. It does not pass through the old head/tail checkpoint clipping.
Model-visible state is redacted with the current workspace policy. Re-reading
is appropriate when evidence is stale or context is missing.

## Scope and limits

This is **checkpoint continuation**, not native session restoration or provider
cache transfer. It carries structured investigation state and the changed
worktree into a fresh host launch. Only one prewalk handoff is offered per
node, and only when another attempt and a cheaper model are available. The
existing budget check still applies. Local receipts are not an isolation or
authentication boundary against an adversary with the same filesystem access.

Artifacts follow the store's retention policy. Missing evidence refuses the
handoff. A live-model cost/quality comparison has not yet been run. Extra
planning, checks, and prefix processing can outweigh the model-price saving.
