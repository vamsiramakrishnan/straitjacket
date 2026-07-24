# LIVE: Antigravity (Gemini) plans, Claude implements — a real task, green test

**Date:** 2026-07-24 · **Harness:**
[`evals/live_collab_antigravity_claude.py`](live_collab_antigravity_claude.py) ·
**Drives:** `ctx.orchestrator.run_route` (the real closed loop) with a live
launcher · **Models:** `gemini-3.5-flash-lite` (Antigravity's Gemini, via
`GEMINI_API_KEY`) + `claude` Sonnet (Claude Code CLI, **no API key** — runs
authenticated as-is with its full Bash/Read/Edit tools)

This is the live counterpart to the offline
[`orchestrator-cost-routing`](orchestrator-cost-routing-2026-07-24.md) receipt.
It answers one question directly: **do two real, different-vendor harnesses
actually collaborate through the orchestrator to produce a verifiable
deliverable?** Yes — a failing test goes green.

## What ran

The orchestrator's own `run_route` closed loop, unchanged, with the injected
fake launcher swapped for a **real** one:

- **antigravity nodes → the Gemini API** (`GEMINI_API_KEY`) — the model
  Antigravity runs. The `agy` CLI needs interactive Google OAuth (see
  [`antigravity-gemini-2026-07-19.md`](antigravity-gemini-2026-07-19.md)), so
  the API is how Antigravity's model is driven headless.
- **claude nodes → `claude -p … --output-format json`** with its real tools
  (`--permission-mode acceptEdits --allowedTools Edit Write "Bash(python*)"
  "Bash(pytest*)"`) so it edits files and runs the test itself. No
  `ANTHROPIC_API_KEY` — the CLI is authenticated in the environment.

A throwaway git repo holds a **failing test**: `strings.longest_run(s)` must
return the `(char, count)` of the longest run of a repeated character. The
route: **plan** (Gemini, economy) → **implement** (Claude, standard),
`implement` ⇐ `plan`. Success is verifiable: after the run, `pytest` in the
scratch repo must be green.

> This route is a **cross-vendor handoff demo, not the cost-optimal routing.**
> The default policy is plan→flagship (Opus), implement→cheap model (see the
> [cost-routing receipt](orchestrator-cost-routing-2026-07-24.md)); here the
> tool-using implement node *must* be Claude (the live Gemini driver is a
> text-only API call and cannot edit files or run pytest), so the cheap Gemini
> node takes the plan slot. The point being proven is the live handoff + a
> real green test across two vendors, not the dollar-optimal assignment.

## Result (real numbers)

```
routing (2 nodes, 2 waves):
  plan       → antigravity/gemini-3.5-flash-lite (economy)  [served as-is]
  implement  → claude/claude-sonnet-4.6          (standard) [→ sonnet]  ⇐ plan

outcomes:
  plan       antigravity/gemini-3.5-flash-lite  [ok] checkpoint:f876b2266f17
  implement  claude/claude-sonnet-4.6           [ok] checkpoint:1ee5efb2e577

handoff proof: the implement node's prompt carried plan's checkpoint digest.

Claude's edit (strings.py) — a real algorithm, not the NotImplementedError stub:
  def longest_run(s):
      if not s: return ("", 0)
      best_char, best_count = s[0], 1
      ...

verifiable deliverable:  pytest in scratch repo: GREEN ✓

real usage / cost:
  antigravity/gemini-3.5-flash-lite   in=191  out= 86   $0.0001   (usage x price table)
  claude/sonnet                       in= 10  out=698   $0.1127   (Claude-reported total_cost_usd)
  TOTAL                                                  $0.1129
  providers exercised: {anthropic, gemini}
  RESULT: PASS
```

## What this proves

1. **Cross-vendor collaboration produces a real, verified deliverable.** Gemini
   planned; Claude — running as-is with its own tools — edited the file and ran
   pytest; the test is genuinely green (checked outside the model).
2. **The handoff is the CAS checkpoint.** `run_route` wrote Gemini's plan to a
   `blob:` + `checkpoint:`, and the Claude node's prompt contained that digest —
   verified in-harness, not asserted.
3. **Escalation works live.** The first attempt used
   `--dangerously-skip-permissions`, which Claude Code refuses under root; the
   Claude node exited non-zero, and `run_route` **caught it and escalated the
   node to the next tier** (gemini-3.1-pro) before the flags were corrected —
   the failure-escalation path firing on a real failure, not a simulated one.
4. **Cost is measured, not estimated, here.** Claude's dollar cost is its own
   `total_cost_usd`; Gemini's is `usage × ctx.pricing`.

## What this does NOT yet prove (honest scope)

- **Not a full A/B.** This shows the collaboration produces a correct result; it
  does not yet compare billed tokens against a single-model baseline on a hard
  task. That larger A/B remains the TO-BUILD in
  [`orchestrator-cost-routing`](orchestrator-cost-routing-2026-07-24.md).
- **Antigravity via the API, not the `agy` CLI.** Antigravity's CLI is
  OAuth-only, so this drives Antigravity's *model* through the API. Driving the
  Antigravity CLI itself headless is still unverified.
- **Codex not exercised** (not installed here). Its `exec` argv/flags are
  unverified.

## Gap it closed

The first live run failed the model ids: the catalog's `claude-haiku-4.5` is
rejected by the CLI (which wants the alias `haiku`), and the economy Gemini was
mis-named `gemini-3.6-flash-lite` — the served model is `gemini-3.5-flash-lite`
(only *flash* is 3.6). Fixed two ways: the flash-lite entry now uses the correct
served id directly, and `ModelChoice.cli_id` carries a launch-time id where it
still differs from the display id (Claude → `haiku`/`sonnet`/`opus`;
`gemini-3.1-pro` → `gemini-3.1-pro-preview`). The registry hands `run_route` the
id the provider actually serves; the re-run above passed with no mapping in the
eval.

## Reproduce

```bash
GEMINI_API_KEY=... python evals/live_collab_antigravity_claude.py   # ~11 cents
```

The Claude node runs with `acceptEdits` + a narrow tool allowlist inside a
`mktemp` throwaway git repo — never your real workspace.
