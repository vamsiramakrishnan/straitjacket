# LIVE: Antigravity (Gemini) + Claude collaboration — real tokens

**Date:** 2026-07-24 · **Harness:**
[`evals/live_collab_antigravity_claude.py`](live_collab_antigravity_claude.py) ·
**Drives:** `ctx.orchestrator.run_route` (the real closed loop) with a live
launcher · **Models:** `gemini-3.5-flash-lite` (Antigravity's Gemini, via
`GEMINI_API_KEY`) + `claude` haiku (Claude Code CLI)

This is the live counterpart to the offline
[`orchestrator-cost-routing`](orchestrator-cost-routing-2026-07-24.md) receipt.
It answers one question directly: **do two real, different-vendor harnesses
actually collaborate through the orchestrator, handing off addressed evidence?**
Yes.

## What ran

The orchestrator's own `run_route` closed loop, unchanged, with the injected
fake launcher swapped for a **real** one:

- **antigravity nodes → the Gemini API** (`GEMINI_API_KEY`) — the model
  Antigravity runs. This is the headless-driveable path; the `agy` CLI needs
  interactive Google OAuth (see [`antigravity-gemini-2026-07-19.md`](antigravity-gemini-2026-07-19.md)),
  so the SDK/API is how Antigravity's model is driven headless.
- **claude nodes → `claude -p … --output-format json`** (Claude Code,
  authenticated in the environment).

A two-node route — **plan** (Gemini, economy) → **implement** (Claude, economy),
`implement` depends on `plan` — on the task *"write an iterative `fib(n)` with a
docstring."*

## Result (real numbers)

```
routing (2 nodes, 2 waves):
  plan       → antigravity/gemini-3.6-flash-lite (economy)   [→ gemini-3.5-flash-lite]
  implement  → claude/claude-haiku-4.5           (economy)   [→ haiku]  ⇐ plan

outcomes:
  plan       antigravity/gemini-3.6-flash-lite  [ok] checkpoint:b971dcbf9165
  implement  claude/claude-haiku-4.5            [ok] checkpoint:95df24b7f28e

handoff proof: the implement node's prompt carried plan's checkpoint —
  "[ctx checkpoint:b971dcbf9165] goal: node plan … state: 1. Inspect repo:fib.py …"

real usage / cost:
  antigravity/gemini-3.5-flash-lite   in=106  out= 70   $0.0001   (usage x price table)
  claude/haiku                        in= 26  out=742   $0.0213   (Claude-reported total_cost_usd)
  TOTAL                                                  $0.0214
  providers exercised: {anthropic, gemini}
  RESULT: PASS
```

## What this proves

1. **Cross-vendor collaboration is real.** Gemini and Claude ran the two nodes;
   both providers were exercised; the run completed green.
2. **The handoff is the CAS checkpoint.** `run_route` wrote Gemini's output to a
   `blob:` + `checkpoint:` and the Claude node's prompt contained that
   checkpoint digest — verified in-harness, not asserted.
3. **Cost is measured, not estimated, here.** Claude's dollar cost is its own
   `total_cost_usd`; Gemini's is `usage × ctx.pricing`.

## What this does NOT yet prove (honest scope)

- **Not a full A/B.** This shows the collaboration runs; it does not yet compare
  billed tokens for the collaboration against a single-model baseline on a hard
  task. That larger A/B remains the TO-BUILD in
  [`orchestrator-cost-routing`](orchestrator-cost-routing-2026-07-24.md).
- **Antigravity via the API, not the `agy` CLI.** The orchestrator's default
  `_launch_host` shells a CLI; Antigravity's CLI is OAuth-only, so this harness
  drives Antigravity's *model* through the API (the SDK path). Driving the
  Antigravity CLI itself headless is still unverified.
- **Codex not exercised** (not installed here). Its `exec` argv/flags are
  unverified.

## Gap it closed

The first live run failed the model ids: the catalog's `claude-haiku-4.5` is
rejected by the CLI (which wants `haiku`), and `gemini-3.6-flash-lite` is not
served (the API has `gemini-3.5-flash-lite`). Fixed by adding
`ModelChoice.cli_id` — the id passed to the provider at launch — verified against
both live drivers. The registry now hands `run_route` the id the provider
actually serves; the re-run above passed with no mapping in the eval.

## Reproduce

```bash
GEMINI_API_KEY=... python evals/live_collab_antigravity_claude.py   # ~2 cents
```
