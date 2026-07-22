# Replacement surface — collapse substitution, first measured run

**Date:** 2026-07-22 · **Model:** claude-haiku-4-5 · **N:** 1 per cell ·
**Scenarios:** the 6 flood/navigation cells (comp-grep, comp-refs, comp-nav,
comp-trace, bug-flood, data-aggregate) · **Arms:** naive · sj (guarded default)
· sj-collapse (`guard.collapse=true` + native Grep/Glob removed).

## What was being tested

The 42-cell benchmark showed parity, diagnosed as a *delivery* failure: the
collapse engine (`ctx q`) shipped as an opt-in verb the model declined. The
replacement surface (`docs/REPLACEMENT-SURFACE.md`) delivers it by substitution
— transparent rewrite of a recognised loop-shape under the agent's own command,
plus removal of the native search tools so search is forced onto the doors we
control. The falsifiable claim: **on the loop-dominated tasks, substitution
fires and turns parity into a win.**

## Results

| arm | success | med turns | med cost | cache-hit % |
|---|---|---|---|---|
| naive | 83% (5/6) | 4.0 | $0.046 | 91.9 |
| sj (guarded) | 83% (5/6) | 5.5 | $0.050 | 96.3 |
| sj-collapse | 100% (6/6) | 5.0 | $0.045 | 95.1 |

Per-scenario cost (naive / sj / sj-collapse), ✅=solved:

| scenario | naive | sj | sj-collapse |
|---|---|---|---|
| bug-flood | ✅ $0.059 | ✅ $0.071 | ✅ $0.075 |
| comp-grep | ✅ $0.061 | ✅ $0.094 | ✅ $0.092 |
| comp-nav | ✅ $0.051 | ✅ $0.042 | ✅ $0.046 |
| comp-refs | ✅ $0.041 | ✅ $0.049 | ✅ $0.044 |
| comp-trace | ✅ $0.024 | ✅ $0.050 | ✅ $0.041 |
| data-aggregate | ❌ $0.018 | ❌ $0.042 | ✅ $0.036 |

## The honest read: no distinguishable effect at N=1

The surface line looks like a win — 100% at naive-parity cost — but the
mechanism telemetry says it is **mostly noise**, and it would be dishonest to
claim otherwise:

- **`collapse_fires` = 1 across all six cells** (a single `grep_symbol` in
  comp-trace). Even with native Grep/Glob removed, the substitution almost
  never triggered — the agent didn't emit bare recursive Bash greps.
- **Verb adoption was flat**: sj made 5 `ctx` verb calls total, sj-collapse
  made 4. Removing the native search tools did **not** push the agent onto the
  collapsed verbs more than the guarded default did.
- **The lone success difference is variance.** data-aggregate flipped to
  solved under sj-collapse — but that run used **Bash only, zero `ctx`
  machinery, zero collapse fires**. It is a counting task the agent happened to
  get right in one draw, not the replacement surface working.

So at N=1 the benchmark cannot separate sj-collapse from noise. The one clean,
repeatable signal is negative-space: **cache alignment held** (95.1%, on par
with sj's 96.3%), confirming that substitution and tool-removal do not disturb
the prompt cache.

There is a *weak, consistent* directional hint — sj-collapse trimmed sj's
overhead back toward naive on 4 of 6 cells (comp-nav/refs/trace, data-agg) —
but it sits inside N=1 error and rides on one substitution fire, so it is a
hypothesis, not a result.

## What is proven vs. what is not

Proven:
- The replacement surface is **correct and safe**: substitution is
  compound-command-safe, the collapsed op is a working equivalent (verified
  live), native-search removal works across all three harnesses, cache
  alignment is undisturbed. 44 tests green.
- When substitution *does* fire (comp-trace), the cell succeeds and costs less
  than guarded sj.

Not proven (and not to be claimed):
- That the replacement surface improves success or cost. At N=1 with
  `collapse_fires`=1 and flat adoption, the apparent win is indistinguishable
  from variance.

## Next test (to actually settle it)

1. **Repeats.** N≥5 per cell to average out the counting-task variance that
   produced the data-aggregate flip.
2. **Harder, loop-dominated tasks.** These fixtures are small enough that the
   emission gate alone bounds the flood; the method-collapse has little left to
   save. A fair test needs tasks where the search-read loop genuinely
   dominates (large repo, many-file trace) so a fired substitution can matter.
3. **Force the fire.** Instrument why bare recursive greps are rare even with
   Grep removed — the agent reaches for `ctx` verbs directly or reads files, so
   the transparent-substitution path is a smaller lever than tool-removal +
   redirect. The honest lead hypothesis is now: **removal and redirect drive
   adoption; transparent substitution is a backstop, not the main event.**
