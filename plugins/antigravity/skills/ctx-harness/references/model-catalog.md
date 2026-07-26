# Model catalog — the routing dimensions beyond price

Load this when you are **choosing between two models that both clear the
capability bar**. If you only need "which tier does this node want", the table
in [`harness-collaboration.md`](harness-collaboration.md) is enough and this
file is not worth its tokens.

Source of truth: `ctx/data/model-catalog.json` (overridable per repo with a
`.ctx-catalog.json`). Tiers and role coverage live in `ctx/hosts.py`; prices in
`ctx/data/model-prices.json`. `ctx wrap detect` prints the live join.

## The rule that matters

**Every quantitative claim carries a `source`, and a claim without one is not
shipped.** `tests/test_model_catalog.py` enforces it. This is not bureaucracy:
a routing decision made on an invented benchmark is worse than one made on
price alone, because it *looks* informed while being fiction.

Two consequences you must reason with:

- **Absent data means UNKNOWN, never bad.** A model with no throughput row has
  not been measured; it is not slow. `speciality_score` returns a neutral 0 for
  a model with no catalog row, and unknown latency sorts as `moderate`, never
  optimistically as `fast`.
- **`declared-heuristic` is an honest source value.** It means a human made a
  judgement call and you may overrule it with a measurement.

## Dimensions

| dimension | what it is | how to weigh it |
|---|---|---|
| `specialities` | declared work this model is good at | primary tie-break once tier is met |
| `anti_specialities` | declared poor fits | strong negative; never a hard block |
| `latency_class` | `fast` / `moderate` / `deliberate`, time-to-first-token *feel* | matters for interactive and fan-out nodes, not for a single long build |
| `throughput_output_tok_s` | **measured** output tokens/second | matters when a node emits a lot (big refactor, long file) |
| `benchmarks` | sourced suite scores | weak proxy — it measures a suite, not your repository |
| `observed_behaviour` | measured conduct from this repo's receipts | often the most decisive row; see below |

## What is actually measured today

Only two models have measured throughput, both from single-agent runs where
wall time covers the agent and nothing else:

| model | output tok/s (median, n=8) | receipt |
|---|--:|---|
| `gemini-3.6-flash` | 91.3 | [antigravity tier receipt](../../../../../evals/antigravity-3.6-flash-vs-3.5-flash-lite-2026-07-25.md) |
| `gemini-3.5-flash-lite` | 58.8 | same |

The Claude arms are deliberately **absent**: the only wall-clock data this repo
has for them mixes model time with Playwright grading, and publishing that as
throughput would be false precision.

`benchmarks` maps ship **empty** for every model. Public scores for these
versions are not in this repo's evidence base, and a fabricated number would be
indistinguishable from a real one at the point of use.

## Observed behaviour — the rows that change routing most

These come from this repo's own A/Bs and are usually more decisive than any
tier label:

- **`gemini-3.5-flash-lite` has low flood discipline.** On a greppable needle
  task it re-ran a 4,000-line log dump 27 times: 7.8 MB of shell output, 1.5M
  tool-output tokens. Route flood-prone work here **only behind containment**.
- **`gemini-3.6-flash` has high flood discipline.** On the same task it chose to
  grep, emitting 812 bytes for the whole task. On a non-greppable task it floods
  like anything else.
- **Cheapest per token ≠ cheapest arm.** As an agentic builder, `gemini-3.6-flash`
  re-sent 4.25M input tokens to produce 63k of output on a three-phase web
  build, landing within $0.34 of an all-Claude arm at a fraction of the unit
  price. **Weigh context growth, not just unit price.**
- **Splitting plan from build lost nothing.** Opus-plans/Sonnet-builds and solo
  Opus both scored 98% on a three-phase reshape; the split cost $7.04 against
  $9.59.

## How to use this in a routing decision

1. Gate on capability tier first (`min_tier`) — capability is a gate, not a score.
2. Among survivors, rank by `speciality_score` against the node's `need_tags`.
3. Break remaining ties on **price**, unless the node is latency-sensitive
   (fan-out, interactive) or output-heavy, where `latency_class` and
   `throughput_output_tok_s` earn their place.
4. Check `observed_behaviour` before routing anything flood-prone to a model
   with low flood discipline.
5. If a dimension is absent, treat it as unknown and fall back to the previous
   step. Never invent a value to break a tie.

## Extending it

Add or refine a row in `ctx/data/model-catalog.json`, or a `.ctx-catalog.json`
in your repo (merged per `match`, so you can tune one model without restating
the table). A benchmark row must look like:

```json
"benchmarks": {"swe-bench-verified": {"score": 0.71, "source": "https://…", "date": "2026-07"}}
```

`lint_catalog()` rejects a score with no source, and `ctx doctor` surfaces the
same check.
