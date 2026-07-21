# The replacement surface

> Adoption is won by substitution, not persuasion.

## The problem this solves

straitjacket's 14-scenario × 3-arm benchmark (`evals/bench-*`) landed at
**parity**: same task success as a naive `claude -p`, ~20% higher cost, the
wins confined to two retrieval tasks. The instinct is to read that as a
capability failure. The ledgers say otherwise.

Across all 14 straitjacket cells the agent invoked a `ctx` verb **three
times**. The collapse engine — `ctx q` with a symbol index, bounded search,
and a failure slice — was present, carded, and hooked, and the model reached
for raw `grep`/`cat`/`pytest` anyway. The hooks *did* fire: they captured
every flood at the emission gate (output stayed bounded) and emitted a hint
toward the cheaper path. But the hint is advisory, and **the model declined
it every time** (`hinted_landed = 0`). The one lever that could compel a
method change — rewriting the command — was in shadow mode by design.

So the gap is not capability. It is **delivery**. We built an engine and
shipped it as a verb the model had to *choose*, then measured a model that
didn't choose it.

## What the field already knew

The neighbouring tools that win adoption do not teach. They **replace the tool
surface**:

- **wozcode** collapses a 12-call find-and-edit into two by giving the agent
  custom tools that *are* the collapsed op — the efficient path is the only
  path. Reported −55% tokens, −40% wall, +11 on Terminal Bench 2.0.
- **Maki** hands the agent one script that does N ops over a tree-sitter
  skeleton, so the whole read-navigate loop is a single call.

Their shared move: the agent never decides to be efficient; the surface it is
given is already efficient. straitjacket had the better *engine* (addressable,
lossless, span-precise) and the worse *delivery* (an opt-in CLI verb).

## The mechanism

The replacement surface delivers the collapse the straitjacket way — **not** a
new tool schema the agent calls (that would add to the very tool-schema mass
that already dominates the window; see `evals/context-accounting-*`), but a
**transparent substitution under the agent's own shell command**.

When the agent runs a recognised loop-shape, the PreToolUse hook rewrites the
command in place, via the existing `updatedInput` path, to the collapsed
`ctx q` op that answers the same question in one bounded, addressable call:

| the agent runs | it actually runs | rung |
|---|---|---|
| `grep -rn Symbol .` (recursive, identifier) | `ctx q 'refs Symbol \| group file'` | reuse-index |
| `grep -rn "pattern" .` (recursive, content) | `ctx q 'search pattern \| files'` | bounded-search |
| `pytest` (whole suite, after a captured failure) | `ctx q 'fails last \| in-changed'` | failure-slice |

The efficient path is taken *for* the model, not left for it to choose — and
because the substitution rides the command the agent already issued, it costs
**zero** additional tool-schema tokens in the window.

## What each neighbour contributes

The surface is one mechanism carrying every good idea the field produced,
each as a property of the substituted op:

| contributes | from | as a property of the surface |
|---|---|---|
| collapse 12 calls → 2 | wozcode / Maki | the substituted op *is* the collapsed call |
| lossless, addressable | Headroom / Caveman | every result is a digest + resolvable handle |
| failure-asymmetric budget | rtk | spend bytes on failures, starve success noise |
| solution ladder, measured | Ponytail | cheapest rung first; ctx-debt logged on defer |
| composition without code-exec | TokenSave | the collapsed ops compose but stay Total |

## Design constraints

- **Pure and total** (`src/ctx/substitute.py`). The recogniser is a token scan
  and a table — no I/O, no store access, no shell-out. It cannot hang or
  flood, and it is trivially testable (`tests/test_substitute.py`).
- **Conservative.** Anything ambiguous returns `None` and the command runs
  untouched — and is still bounded at the emission gate, so a missed
  substitution costs at most one bounded digest, never a flood.
- **Cheapest rung first.** Recognisers are ordered so the first match is the
  cheapest equivalent op; each carries its rung for the ctx-debt ledger.
- **Off by default.** Enabled per-repo via `[guard] collapse = true` in
  `ctx.toml`. The substitution is only ever an *equivalent* of what the agent
  asked for; when in doubt, it does nothing.

## Safety argument

Substituting an agent's command is the highest-authority thing the hook does,
so the bar is equivalence, not improvement:

1. The collapsed op must answer the *same question*. `refs Symbol` returns the
   definition and every use of `Symbol` — a superset of what `grep -rn Symbol`
   finds, span-precise and grouped. `search pattern` returns the same matches,
   bounded with a handle to page the rest. `fails last | in-changed` returns
   the failing cases from the run the agent already executed — it never
   re-runs anything.
2. The result is **lossless**: every omitted byte has a resolvable handle, so
   the substitution can never silently drop evidence the grep would have shown.
3. It is **verified end-to-end**: the live PreToolUse hook rewrites a recursive
   grep to a working `ctx q refs` (exit 0, real sites, a blob handle) on a cold
   workspace with no warmed index.

## Measurement

The `sj-collapse` benchmark arm (`evals/bench_run.py`) is `sj` plus
`[guard] collapse = true`, run against `naive` and the guarded `sj` default on
the flood-bearing and navigation scenarios. The row carries `collapse_fires`
(loop-shapes collapsed, by shape) so adoption is measured directly — the
numerator the old vocab metric missed. The claim to test is narrow and
falsifiable: **on the navigation/flood tasks, substitution makes the collapsed
op actually run, and cost/turns fall below the guarded default.** If it does
not, the engine — not just the delivery — is in question.
