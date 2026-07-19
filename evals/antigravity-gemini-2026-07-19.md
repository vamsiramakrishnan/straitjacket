# Antigravity host A/B — naive vs straitjacket on a long flood task

**Date:** 2026-07-19 · **Host:** Google Antigravity Agent SDK
(`google-antigravity`) · **Model:** `gemini-3.5-flash` · **Auth:**
`GEMINI_API_KEY` (headless) · **Harness:**
[`evals/antigravity_sdk_eval.py`](antigravity_sdk_eval.py) · **Records:**
[`…-quiet.json`](antigravity-gemini-2026-07-19-quiet.json) ·
[`…-keyword.json`](antigravity-gemini-2026-07-19-keyword.json)

This is the first straitjacket eval run against an **Antigravity** host rather
than Claude Code. It measures the same naive-vs-straitjacket question the A/B
suite asks, but through Google's Antigravity agent loop driven by a Gemini API
key.

## Getting to a driveable Antigravity host (setup receipt)

Three access paths were tried; only the third is headless-driveable here.

| Path | Result |
|---|---|
| `agy` CLI (`antigravity.google/cli/install.sh`, v1.1.4) | Installs, has a headless `-p/--print` mode, **but authenticates only via interactive Google OAuth** (`/dev/tty`, 60 s timeout). It does **not** accept `GEMINI_API_KEY`. Not driveable in a headless container. |
| Generative Language API, `antigravity-preview-05-2026` / `gemini-3-pro-preview` | Both reject classic function calling ("Function calling is not enabled" / "use the Interactions API"). Not usable as a plain tool-loop host. |
| **`google-antigravity` SDK** (PyPI) | Default model `gemini-3.5-flash`, authenticates via **`GEMINI_API_KEY`**, ships its own `localharness` agent binary, and exposes `tools`/`hooks`/`policies`. **This is the host used below.** |

Plugin-side setup is independently verified: `ctx antigravity install` renders
the workspace plugin, `ctx doctor --antigravity` passes all 15 checks, and the
MCP server the IDE connects to (`ctx mcp --bounded-only`) answers `initialize` +
`tools/list` with the `ctx` tool.

## Design: one variable

Both arms are the **same** Antigravity SDK `Agent`: same model, same system
instructions, same task, same fixture, same builtin file tools, same
`allow_all` autonomous policy. The single difference is the `shell` tool:

- **naive** — `shell(cmd)` returns raw combined stdout+stderr. Long output lands
  in the transcript and is re-sent on every subsequent model turn.
- **sj** — `shell(cmd)` runs the command through `ctx run` and returns the
  bounded digest; a second `ctx_query` tool resolves any omitted bytes by the
  address the digest prints. (The Antigravity SDK's `PostToolCall` hook is
  inspect-only, so containment is applied at the tool boundary rather than as
  the IDE plugin's PostToolUse rewrite — same birth-gate effect.)

The task is a genuine long agentic job with an engineered flood: a failing test
(`tests/verify.py`) whose root cause is one anomalous line buried in a 4,000-line
(~92k-token) diagnostic log. Two scenarios differ only in whether that line is
greppable:

- **keyword** — the needle announces itself (`"INCIDENT NOTE: …"`).
- **quiet** — the needle is structurally rare but lexically normal: it looks
  exactly like the other 3,999 `INFO … gateway=primary …` lines except it reads
  `gateway=legacy` for one `region=apac` request. Keyword grep for
  `incident|error|note|warn` returns **0 lines**.

## Results (4,000-line flood, both scenarios)

### Scenario `quiet` — the flood is unavoidable (containment win)

| metric | naive | straitjacket | delta |
|---|--:|--:|--:|
| billed total tokens | 226,702 | **158,072** | **1.4× less (−30%)** |
| · input (resend) | 223,835 | 155,158 | 1.4× less |
| · output | 788 | 939 | — |
| **tool-output tokens into context** | **75,935** | **498** | **152× less** |
| raw command bytes produced | 368,366 | 368,533 | ~equal (both ran the full log) |
| tool calls | 8 | 9 | — |
| wall seconds | 24.9 | 27.8 | — |
| **correct** (tests pass) | ✅ | ✅ | tie |
| needle cited | ✅ | ✅ | tie |

With no keyword to grep, the naive agent dumped the whole log — **75,935 tokens
of raw output into context**, re-sent each turn, for 226,702 billed tokens.
straitjacket ran the identical command but returned a **498-token digest** that
surfaced the anomalous line automatically (`logtemplate/v1` selects it as the
structurally-exceptional row), so the same fix landed at **−30% total tokens and
152× less tool-output**, with the needle preserved verbatim at an address:

```
[ctx run:… profile=logtemplate/v1]
stdout: 4,000 lines · 359.4 KiB · est 92,000 tokens
templates: 2 cover 4,000/4,000 lines
  3,999× L1: [<*>] INFO worker-<*> … region=us gateway=primary latency=<*> ok
      1× L2137: [<*>] INFO worker-<*> … region=apac gateway=legacy latency=<*> ok
exceptional:
  L2137: [002137] INFO worker-1 … region=apac gateway=legacy latency=7ms ok
next:
  ctx get run:…#stdout --lines 2137:2140
```

### Scenario `keyword` — the flood is greppable (honest counter-case)

| metric | naive | straitjacket | delta |
|---|--:|--:|--:|
| billed total tokens | **99,964** | 159,389 | naive 1.6× cheaper |
| tool-output tokens into context | 157 | 1,110 | — |
| raw command bytes produced | 550 | 389,028 | — |
| tool calls | 6 | 9 | — |
| **correct** | ✅ | ✅ | tie |

When the needle announces itself, the Antigravity agent is shell-savvy enough to
`python3 diagnose.py | grep "INCIDENT NOTE"` and never floods (550 raw bytes).
straitjacket's extra tool + digest overhead then makes it the *more* expensive
arm at equal correctness. This is the same **parity/loss regime** the README
records for pipe-filterable and tiny-surgical tasks — reported here, not hidden.

## What this shows

- straitjacket's containment transfers to a non-Claude host: on the same
  Antigravity agent loop, an unavoidable flood costs **−30% total / 152× less
  tool-output** at equal correctness, with the decisive line kept at an address.
- The win is conditional, exactly as the doctrine claims: when the agent can
  cheaply pre-filter the flood itself (a greppable keyword), containment is pure
  overhead and naive wins. Both regimes are real and both are measured.
- The billed-token floor is dominated by the Antigravity SDK's fixed
  system+tools prefix (~13–25k tokens resent per turn), which is identical
  across arms; the measured delta is the tool-output containment on top of it.

## Reproduce

```bash
pip install google-antigravity           # SDK (venv recommended)
pip install -e .                          # ctx, on PATH
export GEMINI_API_KEY=...                 # AI Studio key
python evals/antigravity_sdk_eval.py --scenario quiet   --flood-lines 4000
python evals/antigravity_sdk_eval.py --scenario keyword --flood-lines 4000
```

Both arms run sequentially in isolated fixtures; per-arm `UsageMetadata` is the
billed-token source, and correctness is an independent `tests/verify.py` re-run.
