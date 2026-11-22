# Antigravity SDK worker

A shim over the **`google-antigravity` Agent SDK**. Its asynchronous tools use
the provider-independent [Tool SDK](../../docs/TOOL-SDK.md), including typed
command status, bounded retrieval, cancellation and capsule export.

## Choose a worker

Use native `agy` for interactive work or a resumable CLI conversation. It
can rewrite selected tool inputs. Its post-tool hook leaves output in the
transcript. The SDK worker owns its command tools and bounds their results
before returning them to the model. It authenticates with `GEMINI_API_KEY`.
See [Gemini model selection](../../docs/NATIVE-AGY-SESSIONS.md#choose-a-gemini-model)
for Hydrogen, Neon, Carbon and Argon.

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

This run used 17% fewer input tokens. Both arms answered correctly. The sample
contains one task on Gemini 3.6 Flash. The [tier comparison](../../evals/antigravity-3.6-flash-vs-3.5-flash-lite-2026-07-25.md)
shows how often the agent searched the log in that experiment.

## Managed install

The shim is wired as the distinct, first-class `antigravity-sdk` host. Install
or repair its isolated environment with:

```bash
ctx wrap antigravity-sdk
```

This never replaces the vendor `agy` CLI and is intentionally excluded from
`ctx setup`, because creating a virtualenv and downloading an SDK is an
explicit networked action.
