# Antigravity harness, with and without straitjacket — Gemini 3.6 Flash vs 3.5 Flash-Lite

**Date:** 2026-07-25 · **Host:** Google Antigravity Agent SDK (`google-antigravity`),
headless via `GEMINI_API_KEY` · **Runner:**
[`antigravity_sdk_eval.py`](antigravity_sdk_eval.py) ·
**Aggregator:** [`agy_ab_matrix.py`](agy_ab_matrix.py) · **Raw records:**
`evals/_runs/agy-*/records.json`

Two arms, one variable: whether the agent's `shell` output is contained by
straitjacket (`ctx run` digest + `ctx_query` retrieval) or returned raw and
re-sent on every subsequent model turn. Everything else — SDK, model, system
instructions, task, fixture, builtin file tools, `allow_all` policy — is
identical.

Task: a failing test whose root cause is one anomalous line buried in a
4,000-line diagnostic log. Two scenarios:

- **quiet** — the needle is structurally rare but lexically normal (one field
  value differs); `grep` for a keyword cannot find it.
- **keyword** — the needle announces itself with a distinctive phrase, so a
  shell-savvy agent can `grep` past the flood entirely.

Run 3× per model on `quiet`, 1× on `keyword`.

## The matrix

| model | scenario | n | arm | billed tokens | tool-output tokens in context | billed $ | correct |
|---|---|--:|---|--:|--:|--:|:--:|
| `gemini-3.5-flash-lite` | keyword | 1 | naive | 384,455 | 1,516,511 | $0.0784 | 1/1 |
| `gemini-3.5-flash-lite` | keyword | 1 | **sj** | 58,306 | 407 | $0.0122 | 1/1 |
| | | | _ratio_ | **6.6× less** | **3726× less** | **6.5× less** | |
| `gemini-3.5-flash-lite` | quiet | 3 | naive | 261,723 | 278,304 | $0.0530 | 3/3 |
| `gemini-3.5-flash-lite` | quiet | 3 | **sj** | 43,972 | 420 | $0.0091 | 3/3 |
| | | | _ratio_ | **6.0× less** | **663× less** | **5.8× less** | |
| `gemini-3.6-flash` | keyword | 1 | naive | 43,795 | 239 | $0.0620 | 1/1 |
| `gemini-3.6-flash` | keyword | 1 | **sj** | 44,589 | 261 | $0.0616 | 1/1 |
| | | | _ratio_ | **1.0× less** | **1× less** | **1.0× less** | |
| `gemini-3.6-flash` | quiet | 3 | naive | 225,190 | 75,993 | $0.2909 | 3/3 |
| `gemini-3.6-flash` | quiet | 3 | **sj** | 57,536 | 429 | $0.0797 | 3/3 |
| | | | _ratio_ | **3.9× less** | **177× less** | **3.7× less** | |

Dollars are the mean billed usage priced at
[`model-prices.json`](../src/ctx/data/model-prices.json) list rates.

## What actually differs between the two models

**Correctness is not the axis.** Every one of the 16 runs fixed the bug and
cited the anomalous region. At this flood size, neither model needed
straitjacket to *succeed* — so the honest claim is about what the success cost,
and how predictable that cost was.

**The difference is flood discipline, and it is large.** On the `keyword`
scenario, where the needle is greppable:

| model | arm | tool calls | raw bytes the shell produced | tool-output tokens into context |
|---|---|--:|--:|--:|
| `gemini-3.6-flash` | naive | 6 | 812 | 239 |
| `gemini-3.6-flash` | sj | 5 | 550 | 261 |
| `gemini-3.5-flash-lite` | naive | 27 | 7,767,076 | 1,516,511 |
| `gemini-3.5-flash-lite` | sj | 6 | 388,453 | 407 |

3.6 Flash worked out on its own that it should `grep` rather than print the log
— it produced **812 bytes of shell output for the entire task**. There was no
flood to contain, so straitjacket bought it nothing (44.6k vs 43.8k billed
tokens — a wash, marginally negative). Flash-Lite, on the same task, ran the
full log dump repeatedly: **27 tool calls, 7.8 MB of raw output, 1.5M tokens of
tool results into context**. Containment cut that to 407 tokens and 6.6× the
billed cost.

**When grep can't save you, containment matters for both.** On `quiet` — where
the needle is not greppable and the log has to be *analysed*, not searched —
3.6 Flash also floods, and straitjacket is worth 3.9× billed tokens and 177×
the tool output entering context. That is the case straitjacket is actually
built for: the flood is not avoidable by being clever, only by being addressed
instead of pasted.

## The result that does not show up in the means: variance

Per-run, `quiet`, all three repeats:

| model | rep | naive billed | naive tool-out | naive raw bytes | sj billed | sj tool-out |
|---|--:|--:|--:|--:|--:|--:|
| 3.6-flash | 1 | 270,935 | 76,034 | 368,628 | 46,095 | 358 |
| 3.6-flash | 2 | 238,684 | 76,012 | 368,628 | 80,787 | 569 |
| 3.6-flash | 3 | 165,951 | 75,934 | 368,366 | 45,725 | 359 |
| 3.5-flash-lite | 1 | 209,033 | 227,811 | 1,105,957 | 43,802 | 353 |
| 3.5-flash-lite | 2 | 162,464 | 75,942 | 368,366 | 43,837 | 353 |
| 3.5-flash-lite | 3 | 413,672 | 531,160 | 2,577,851 | 44,278 | 554 |

The naive arm's cost ranges over **2.5×** on the same task with the same model
(162k → 414k), because whether it floods once or seven times is a coin flip the
model makes at run time. The straitjacket arm sits in a 43.8k–80.8k band with
tool output pinned between 353 and 569 tokens — the digest is a fixed size
regardless of what the command emitted, so the *tail* is bounded, not just the
average. For anyone budgeting agent spend, that predictability is worth more
than the mean.

## Honest limits

- 16 runs total; `keyword` is n=1 per model. The variance table is the point,
  and n=3 is enough to show the naive arm's spread is wide, not to put an
  interval on it.
- Both arms are capped at 300k chars of shell output per call (a guardrail in
  the runner so one pathological command cannot 400 the API). At 4,000 lines the
  naive arm is already being truncated by that cap, and the needle happens to
  survive inside it. A bigger flood would make the naive arm fail for a reason —
  truncation — that is an artifact of the runner, not of the model, so the flood
  was left at a size where both arms genuinely see the evidence.
- `tool-output tokens into context` is measured with the repo's byte-based
  estimator at the tool boundary; billed tokens are the SDK's own
  `UsageMetadata`. They are different instruments and the first exceeds the
  second in the heaviest naive runs, because the SDK drops history the model was
  never re-charged for.
- Containment here is applied at the tool boundary (ctx-routed `shell`), not via
  the IDE plugin's PostToolUse hook, because the SDK's `PostToolCall` hook is
  inspect-only.

## Reproduce

```bash
python -m venv /tmp/agy-venv && /tmp/agy-venv/bin/pip install google-antigravity
GEMINI_API_KEY=... /tmp/agy-venv/bin/python evals/antigravity_sdk_eval.py \
  --out evals/_runs/agy-3.6-flash-quiet --model gemini-3.6-flash --scenario quiet
python evals/agy_ab_matrix.py evals/_runs/agy-*
```
