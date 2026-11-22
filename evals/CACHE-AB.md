# Matched cache, exploration, and containment evaluation

This experiment separates three claims: whether local delivery preserves useful
evidence, whether a model completes the same coding tasks more efficiently, and
whether a stable provider prefix produces actual cache reads. The first claim
has a reproducible offline receipt. The latter two require separately approved
billable execution; the repository contains no live result from this study.

The implementation is `cache_ab.py`, with independent task/referee, tools,
accounting, offline, and replay modules. It reuses the existing
[`coding_suite.py`](coding_suite.py) fixture registry and prompts and
[`field_needle.py`](field_needle.py) corpus. The older optional SDK is imported
only when its existing live runner is invoked, so these fixtures remain usable
without provider packages or credentials.

## What PR #59 established

[PR #59](https://github.com/vamsiramakrishnan/straitjacket/pull/59), merged
2026-11-22, corrected hashing of the exact serialized UTF-8 cache prefix and
ensured prefix, suffix, and joined prompt derive from the same redacted text.
Its 302,628-token field fixture replay and 203 passing focused tests were local
validation. The PR explicitly reports no live inference or provider billing
benchmark. Stable-first layout existed before that fix; a provider caching
advantage cannot be attributed causally to the PR from the fixture result.

The new [offline receipt](cache-ab-2026-11-23.md) was reproduced against current
`main` after the earlier unpushed workspace expired. It supersedes the earlier
local artifact; it is not a recovered byte-identical patch.

## Coding experiment: matched 2 × 2 design

| Arm | Exploration policy | Native command output |
|---|---|---|
| `native` | `ls`, `rg`, `cat`, bounded `sed` | Raw stdout and stderr |
| `explore` | Prefer `ctx map/search/def`; native commands remain available | Raw stdout and stderr |
| `contain` | Native exploration | Real `ctx run`, then addressable `ctx get`/stored-output search |
| `full` | Context-aware exploration, with native commands available | Real `ctx run`, then retrieval |

Containment wraps every native command, including source reads. Context-native
commands are not wrapped a second time. The common model-visible tools are
`shell(command)` and `write_file(path, content)`, with identical JSON schemas.
Arm-specific policies and allowed context commands are the interventions.
The baseline can search and read narrowly; it is not forced to flood its context.

Caching is enabled identically in all coding arms: a stable system breakpoint
and a breakpoint at the last user/tool-result block, both five-minute ephemeral.
Conversation history is append-only. Previous breakpoint positions are not
carried forward; the provider's prefix lookback determines actual reuse. No
automatic compaction, hidden summarization, or padding is applied. Tiny prompts
below the model's cache minimum may correctly report zero reads and writes.

| Existing fixture | Task | Allowed source edits | Role in interpretation |
|---|---|---|---|
| `quiet_flood` | Change APAC routing based on one quiet log line | `app/router.py` | Containment-favourable |
| `keyword_flood` | Same repair, with greppable `FIXME` evidence | `app/router.py` | Search-aware counterfactual |
| `traceback` | Default a missing ratio while preserving supplied values | `pipeline.py` | Diagnostic containment |
| `bigtest` | Repair parity, last-element access, and zero sign | `mathlib.py` | Small failing-suite control |
| `multifile` | Repair lowercasing through a helper call | `api.py`, `textutil.py` | Low-output navigation control |

The default pilot has **5 tasks × 4 arms × 3 repeats = 60 agent executions**.
Task/repeat blocks are shuffled with seed 1729. Four-arm order rotates by block,
placing each arm in each position three or four times across 15 blocks.
Every cell starts from identical fixture bytes, fresh state, and a new candidate
directory. Trial namespaces prevent cache reuse across independent invocations
and cells. Local stores are cold at each cell's start.

Primary contrast: `full − native` within each task/repeat. Component contrasts:
`explore − native` and `full − contain` for exploration; `contain − native` and
`full − explore` for containment. The interaction is
`full − explore − contain + native`. Do not assume additivity. Raw per-cell
records allow all contrasts; the report includes paired baseline comparisons.

Only **five independent fixtures** are represented. Repeats are nested within
them and do not create 60 independent problem samples. Report per-task outcomes
and low-output regressions before an aggregate. This pilot is descriptive; it
does not establish a production-wide quality or cost claim.

## Separate cache-prefix experiment

The first 512 lines of the existing field log form a shared reference. The
actual `ctx.context_compression.cache_wrap` produces its serialized JSON prefix
and suffix. Three queries use exactly the same serialized blocks across layouts:

| Layout | Request block order | Intended behavior |
|---|---|---|
| `stable_first` | Marked reference, then changing query | First cold, later warm |
| `dynamic_first` | Changing query, then marked reference | Prefix changes each query |

Both layouts use the same namespace within a repeat. Their only content
intervention is ordering. A fresh random invocation nonce plus repeat index
prevents stale cache reuse on reruns. The short common system text cannot itself
reach the cache threshold. Layout order alternates between repeats.

**2 layouts × 3 queries × 3 repeats = 18 Messages calls**, each capped at
64 output tokens. Queries are sequential. Record intended cold/warm state and
actual cache reads separately: cache misses, eviction, TTL expiry, and under-
threshold requests are observations, never silently recoded as hits. Five-minute
TTL starts at request start. Counterfactual layouts intentionally churn the
prefix; the coding baseline does not.

The full 302,628-token log exceeds this model's 200K context and remains an
offline fixture. Its output-token reduction is not a provider billing estimate.

## Frozen execution settings

| Setting | Value |
|---|---|
| Provider / endpoint | Direct Anthropic Messages, fixed `api.anthropic.com` origin |
| Model | `claude-haiku-4-5-20251001` |
| API version | `2023-06-01` |
| Sampling / thinking | Temperature 0; thinking disabled; no provider seed |
| Service | `standard_only`; streaming; no batch, fast mode, server tools, or geo override |
| Coding limit | 12 model calls per task; 2,048 output tokens per call |
| Input preflight | Free token-count estimate ≤64,000; conservative reservation independent of estimate |
| Timing | 240-second agent budget, then separate grading; 60-second HTTP budget; 10-second tool budget, clipped to remaining agent time |
| Tool limit | Eight tools per model response; 1 MiB native output limit, explicit failure on overflow |
| Execution | Concurrency 1; retries 0; no implicit resume; append-only conversation |
| Context engines | Python search, builtin map, AST code, local picker |
| Offline tokenizer | `tiktoken==0.11.0`, `o200k_base`; not the provider tokenizer |
| Runtime identity | Python and package versions; actual child PATH; binary paths and SHA256; source-file hashes |

Within a contained tool call, the child command receives a shorter timeout to
leave time for `ctx` to reap it and render evidence: reserve the larger of
0.5 seconds or 20% of the remaining tool budget. With the default ten-second
outer tool limit, the captured command receives eight seconds. Both actual
limits are recorded per tool result.

API version 2026-10-21 rejects sampling parameters; the explicit older version
preserves the requested temperature-zero setting. Model snapshot, availability,
cache minimum (4,096 tokens), and prices were verified on 2026-11-23 using the
[model documentation](https://platform.claude.com/docs/en/models/haiku-4-5/overview),
[prompt-caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[service-tier reference](https://platform.claude.com/docs/en/api/service-tiers),
and [API migration guide](https://platform.claude.com/docs/en/api/migration-2026-10-21).
Reverify availability and prices before approving a later execution. HTTP
timeouts are enforced through socket timeouts and remaining-budget checks
between phases; DNS resolution and a peer that continuously dribbles headers
can overrun wall-clock targets. Budget reservations still limit new admission.

## Metrics and acceptance

Each request retains the exact request JSON, raw SSE, final response JSON,
request ID, count estimate, and reservation/settlement evidence. Provider usage
counters are disjoint: uncached input, cache-creation input, cache-read input,
and output. Total input is the sum of the first three. Streaming usage deltas
are cumulative and overwrite prior values; they are not summed.

Latency records include time to HTTP headers, first response-body byte,
first nonempty text/tool-input delta, request wall time, tool time, and complete
task wall time. Network-inclusive measurements apply to this client/location.
SSE is retained even when partial. A final `message_stop`, final output count,
and expected model are required before pricing. Incomplete tool JSON at an
output limit is retained and never executed.

The independent referee copies only approved source files into a separate
directory and runs frozen public tests plus extra edge cases with isolated
Python and a completion nonce. Candidate edits cannot replace the tests or
silently exit early. Changed immutable files, undeclared files, and symlinks
fail acceptance. All five initial fixtures must fail and all five known repairs
must pass as calibration. This is not a model-success result.

Credential variables and Python injection settings are absent from child
environments. The command grammar disallows arbitrary shell programs, pipes,
redirections, and arbitrary Python snippets. Candidate Python still executes:
this controlled fixture runner is **not an OS sandbox for hostile code**.

Keep failed, capped, interrupted, and unstarted cells. Task acceptance and model
completion are separate fields. Compare latency and token medians only for
complete pairs, and label that conditioning; never drop failure cost from the
study total. Planned-denominator success is a conservative lower bound while
cells remain unstarted. Started-denominator success is reported separately.
Cost per accepted task includes spending on failures; zero successes or
unresolved charges make it undefined. List-price cost is not invoice reconciliation.

## Budget and spending guard

USD per million tokens: **$1 uncached input; $1.25 five-minute cache creation;
$0.10 cache read; $5 output**. One-hour writes or nonzero server-tool charges
are rejected because they are outside this approved rate card.

| Estimate component | Assumption | USD |
|---|---|---:|
| Coding | 60 tasks × 6 calls × (12,000 input × $1.25 + 800 output × $5) / 1M | 6.84 |
| Prefix probe | 18 calls × (10,000 input × $1.25 + 64 output × $5) / 1M | 0.23076 |
| Total expected pilot | All estimated input charged at the write rate | **7.07076** |
| Proposed maximum authorized spending | Local ledger cap | **15.00** |

Before each Messages request the Decimal ledger reserves the entire 200K model
input window at the most expensive allowed input rate plus maximum output:
**$0.26024** per coding request and **$0.25032** per prefix probe. Observed valid
usage settles the reservation. A missing final usage record, transport failure,
unexpected model, or unpriced usage retains estimated exposure and blocks all
further requests. Preflight and evidence-persistence errors also stop the study;
known charges remain accounted even if saving a response fails. There are no
automatic retries. The cap can stop the pilot
before all cells complete; it is not a guarantee of completing all 78 items.
It is a local guard, not a provider-enforced account limit or invoice guarantee.

## Reproduce and review without spending

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]' tiktoken==0.11.0
python evals/cache_ab.py offline --out /tmp/cache-ab-offline-new
python evals/cache_ab.py plan --repeats 3 --cap-usd 15.00 --out /tmp/cache-ab-plan.json
python -m pytest tests/test_cache_ab*.py -q
```

Native tools `ls`, `rg`, `cat`, and `sed` must be available for a complete replay
and live plan. Optional-tokenizer integration tests skip when the pinned
tokenizer is absent; accounting, referee, transport, and approval tests remain
provider-free. No test supplies a real provider client. First use of tiktoken
may download its public tokenizer data; it performs no model inference.

Review `plan.json`: exact source/runtime/fixture/tool hashes, request settings,
schedule, price card, budget, and approval SHA256. A plan made on another
machine must be regenerated and reviewed there because binary identity is
bound. Publishing identical source bytes does not invalidate the plan merely
because Git HEAD changes; the original base and actual execution commit are
both recorded. Changes to executable bytes, fixtures, settings, or runtime do
invalidate it. No existing output directory or approval plan is overwritten.

Only after separate explicit model-spending approval:

```bash
python evals/cache_ab.py live \
  --plan /tmp/cache-ab-plan.json \
  --approve-plan EXACT_APPROVED_SHA256 \
  --accept-spend \
  --out /tmp/cache-ab-live-new
```

The `live` command requires `ANTHROPIC_API_KEY`; neither `offline` nor `plan`
constructs a provider client. Repository publication permission does not
authorize live spending. No workflow automatically runs the live command.

## Existing evidence and its limits

- [`coding-suite-2026-07-20.md`](coding-suite-2026-07-20.md): 20 historical
  live runs, five tasks, two arms, two repeats. Flood tasks benefited; small
  outputs and navigation carried overhead. Different provider/harness and
  aggregate-only historical records cannot substitute for this matched pilot.
- [`matrix-2026-07-18.md`](matrix-2026-07-18.md): prefix/cache warmup was a
  confound; high cache-hit ratio alone did not guarantee lower total cost.
- [`tokenomics/RESULTS.md`](tokenomics/RESULTS.md): smaller channels did not
  consistently improve quality or cost. Do not reuse its unverified price table.
- [`BENCHMARK.md`](BENCHMARK.md) and
  [`contextbench-ab-design.md`](contextbench-ab-design.md): independent
  acceptance and explicit low-output controls guide this design.

Offline reduction supports a channel-efficiency hypothesis. Live task success,
provider cache reuse, latency improvement, and dollar savings remain unmeasured.
