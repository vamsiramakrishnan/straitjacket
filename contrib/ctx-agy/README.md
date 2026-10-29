# `ctx agy` — a headless, harnessed Antigravity CLI

A shim over the **`google-antigravity` Agent SDK**. Its asynchronous tools use
the provider-independent [Tool SDK](../../docs/TOOL-SDK.md), including typed
command status, bounded retrieval, cancellation and capsule export.

## Why

| | official `agy` | this shim |
|---|---|---|
| headless auth | ✗ interactive OAuth only; ignores `GEMINI_API_KEY` | ✓ `GEMINI_API_KEY` |
| birth gate | deny-only (no `updatedInput` in the hook schema) | ✓ contained *inside* the tool |
| output-side gate | ✗ impossible — PostToolUse's only legal output is `{}` | ✓ output is born bounded |

Both `agy` limitations are structural, not oversights: see
[ADR 005](../../spec/adr/005-antigravity-hook-contract.md) and the
[published hook contract](https://antigravity.google/docs/hooks). No hook can
substitute a tool result on that host, so no amount of hook wiring produces an
output-side safety net.

The SDK's own hooks do not solve it either — `PostToolCallHook` is an
`InspectHook`, so like the published contract it observes and cannot replace.
What the SDK *does* give an embedder is **ownership of the tools**. That moves
containment from "fix the result afterwards" (impossible here) to "the result
was never unbounded" — which needs no substitution API at all.

## What it replaces

The default `command-output` profile disables only `RUN_COMMAND` and supplies
three asynchronous tools: `run_command`, `ctx_get`, and `ctx_search`. Native file
reading, discovery and editing remain available. A command result is one
`ctx.command-result/v1` JSON object; small complete stdout/stderr stay separate,
and large output remains retrievable by its full run address.

Use `--tool-profile full` to disable all five flooding builtins and add bounded
file viewing, directory search, directory statistics, corpus file discovery and
finite `ctx_query` composition. Empty files are discoverable. `ctx_query` accepts
the observation pipeline grammar, not arbitrary CLI commands.

`CREATE_FILE`, `EDIT_FILE`, `ASK_QUESTION` and `FINISH` remain native in both
profiles. The caller's SDK policy still owns permission decisions.

## Use

```bash
python -m venv /tmp/agy-venv && /tmp/agy-venv/bin/pip install google-antigravity ctx-harness
GEMINI_API_KEY=... /tmp/agy-venv/bin/python contrib/ctx-agy/ctx_agy.py \
    --add-dir . --model gemini-3.6-flash --evidence-output evidence.tar -p "fix the failing test"
```

It lives in `contrib/` and runs from its own venv because the SDK conflicts with
the system PyJWT — it is deliberately not imported by `ctx` itself. Release
wheels also carry this shim as package data so the managed launcher does not
depend on a source checkout.

`--no-contain` runs the same agent with the native builtins instead, which is
the A/B baseline.

A build containing the public Tool SDK is required; the historical 0.39.0
release cannot import it. The managed launcher loads only the parent ctx package,
keeping its broader site-packages out of the provider environment.

`--usage-input-semantics` defaults to `unknown`; raw prompt, cache, candidate,
thinking and total counters remain available without invented zeroes or a billing
assumption. Use `includes_cache` or `excludes_cache` only with evidence for the
chosen provider path. `--json` includes that usage record.

## Historical measurement

Fixture: a failing test whose cause is one anomalous line in a 4,000-line log
(the `quiet` scenario from
[`antigravity_sdk_eval.py`](../../evals/antigravity_sdk_eval.py)), `gemini-3.6-flash`:

| arm | input tokens | correct |
|---|--:|:--:|
| `--no-contain` (native builtins) | 130,246 | ✓ |
| contained (ctx-backed tools) | 107,900 | ✓ |

**17% fewer input tokens, n=1.** That is a modest delta and it should not be
quoted as the headline: 3.6 Flash is already flood-disciplined on this scenario
(it greps rather than dumps — see the
[tier receipt](../../evals/antigravity-3.6-flash-vs-3.5-flash-lite-2026-07-25.md)),
and the SDK's native `RUN_COMMAND` appears to bound its own output somewhat. The
containment gap widens on weaker models and on genuinely unavoidable floods; it
is near zero on a model that already knows not to paste the log.

The durable result is not the 17%. It is that this makes Antigravity
**harnessable and scriptable at all** — an orchestrator node, a CI job, or a
cron trigger can drive it, which OAuth-only `agy` forbids — and that it is the
only configuration in which that host has an output-side gate.

## Managed install

The shim is wired as the distinct, first-class `antigravity-sdk` host. Install
or repair its isolated environment with:

```bash
ctx wrap antigravity-sdk
```

This never replaces the vendor `agy` CLI and is intentionally excluded from
`ctx setup`, because creating a virtualenv and downloading an SDK is an
explicit networked action.
