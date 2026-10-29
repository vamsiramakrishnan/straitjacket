# Pinned tool replay and session accounting

This runs actual installed tools without a model. It is a capability replay,
not a ranking of agents, whole-session token efficiency or billed cost. The
fixed script knows the symbol/file, repair and log query.

The shared tasks are the nine-file semantic rename from `lsp_edit_comparison`,
a discount arithmetic bug with six behavioral checks, and recovering an exact
quiet failure record among 4,000 log lines. Each arm starts from identical
fixture bytes and uses the same acceptance check. The JSON receipt records
fixture/acceptance hashes, runner hash, all ctx Python source hashes, installed
binary hashes, upstream revisions, tokenizer version and tool requests/results.
Three repeats rotate arm order. Model calls are zero; measured model usage,
turns and dollars are null.

| Tool | Rename | Bug fix | Quiet log record |
| --- | --- | --- | --- |
| ctx CLI, 0.42.0 integration source | Passed | Passed | Passed |
| Serena 2.1.1.dev0, ede329aa | Passed | Passed | Passed |
| TokenSave 7.16.0 | Safely refused ambiguous graph sites | Passed | Passed |
| Headroom 0.42.0, a2d4fe72 | No native edit adapter | No native edit adapter | Passed after CCR retrieval |
| WozCode | Unavailable | Unavailable | Unavailable |

TokenSave's rename refusal is a safety boundary, not a corrupting edit. The
runner does not override `allow_heuristic`. It uses the graph definition's node
ID for rename and its fully qualified name for replacement.

Headroom runs structural compression with `kompress_model="disabled"` and
`protect_recent=0`. Its initial log compression omits the quiet record and
provides a retrieval hash. The adapter follows that hash through its own CCR
store and counts the returned original as retrieval output. This is not its
ML/proxy-default behavior. No hosted or GPU model is called. A compressor needs
a host editor, so editing cells are unsupported, not failed.

WozCode's proprietary Claude Code plugin is not installed/configured here.
There is no substitute implementation and no claimed WozCode result. A live
same-model host comparison remains open for all tools.

Tool outputs use native interfaces: ctx and TokenSave CLI stdout/stderr, Serena
MCP text content, Headroom library output plus CCR retrieval. Schema payloads
are measured where observed, otherwise null. These serializations and operation
boundaries differ. Token counts describe these scripts and must not become a
product savings percentage. All source-changing arms run behavioral checks and
preserve unrelated same-name definitions, comments and strings.

## Reproduce

The paths in `tool-comparison-pins-2026-10-29.json` are local installation paths.
Recreate isolated environments and change those paths and `repo` paths. Checkout
the exact revisions, install the saved requirements, and verify executable
hashes. Serena and Headroom need separate environments because their Click
requirements conflict. TokenSave's v7.16.0 Linux release archive was verified
against its published SHA256:
`783dcea8bb8b472982b497523ad912a5176b8d8e770d25592dc38b5f749ac7ea`.

```sh
PYTHONPATH=src python -m evals.tool_comparison \
  --pins evals/tool-comparison-pins-2026-10-29.json \
  --out evals/tool-comparison-local.json --repeats 3
PYTHONPATH=src python -m pytest tests/test_session_economics.py tests/test_tool_comparison.py
```

The runner refuses changed binary hashes, changed/dirty upstream source and
changes to the pinned loaded Serena/Headroom Python package source.
ctx source hashes identify code under test, including unreleased code. No global
agent integration or git hooks are installed. TokenSave subprocesses disable
update checks and use a closed HTTP proxy to prevent counter uploads.

## Whole-session accounting

`python -m ctx.session_economics events.jsonl --accepted true --out summary.json`
reads `ctx.session-event/v1` events. Model events identify a unique final
response and resolved model. They may contain normalized `usage` or raw
`provider_usage` plus `provider` of `openai`, `anthropic` or `gemini`.

```json
{"schema":"ctx.session-event/v1","kind":"model","id":"response-1","model":"exact-model-id","provider":"openai","provider_usage":{"input_tokens":1000,"input_tokens_details":{"cached_tokens":800},"output_tokens":100,"output_tokens_details":{"reasoning_tokens":80}},"turns":1}
{"schema":"ctx.session-event/v1","kind":"tool","id":"tool-1","argument_tokens":20,"result_tokens":200,"retrieval":true,"retry":false}
{"schema":"ctx.session-event/v1","kind":"tool_schema","id":"schema-1","tokens":300}
```

Do not mix cumulative totals with individual responses or feed every streaming
usage snapshot as a new response. Failed/retried requests must remain in the
event stream. Missing categories and turns stay null. Reasoning is part of
output, never added twice. Cached input is subtracted from inclusive
OpenAI/Gemini prompt counts. Gemini cache storage charges need separate billing
evidence; they are not inferred from token usage.

Optional `--rates rates.json` supplies USD per million tokens for disjoint
`input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`.
No model price is guessed. Cache writes must have a single TTL/rate; declare
mixed `cache_write_ttls` to require provider-reported cost or separately priced
events. Missing price/usage yields null cost. Mixed models reject one shared
rate card. Provider-reported cost can be supplied as `cost_usd` per response.

Schema, retrieval, argument and result counts describe payloads already
included in provider usage. They are never added to the bill. `compare()` only
emits savings for matching fixture, acceptance, model, host and budget identities
with successful tasks and complete usage/cost. Failed, unavailable, unmatched
or partially observed sessions cannot appear cheaper.

## Remaining experiment

Run these tasks under one pinned model/host with native integrations, record
every final provider response including caches/reasoning, and import those
events into the accounting module. Add held-out real bugs and measure recovery
over repeated runs. This replay provides executable adapters and correctness
gates; it does not establish that ctx is cheaper than any named tool.
