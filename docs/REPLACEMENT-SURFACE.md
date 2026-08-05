# The replacement surface

> **Design & internals — not product documentation.** This doc explains *why* a
> mechanism exists and how it was reasoned out; it may describe an idea before it
> ships or record one that was rejected. For what the product does **today**,
> prefer [`spec/`](../spec/) and the [changelog](../CHANGELOG.md), and read any
> status label literally. New to the vocabulary? Read
> [How it works](HOW-IT-WORKS.md) and [Concepts](CONCEPTS.md) first.

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
- **The default posture — one mode, not two.** The replacement surface *is*
  the harness; there is no opt-in flag to remember and no second product to
  choose between. Safety is by construction rather than by a fallback mode:
  a symbol grep degrades to bounded content search when the repo can't resolve
  refs (`_symbols_resolvable` — a SCIP index or Python sources), and Bash grep
  always remains, so the agent is never stranded. `[guard] collapse = false`
  in `ctx.toml` is a break-glass off-switch for emergencies, not a supported
  operating mode.

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

## Coverage across harnesses

Transparent substitution only works where the agent's action is a **shell
command string** the hook can rewrite in place. A host's *own* search tool
(Claude Code's `Grep`/`Glob`) is a distinct tool call, not a command, so
`updatedInput` cannot swap it into a `ctx q` invocation — it can only cap it.
The first collapse benchmark proved the consequence: the agent searched with
the native `Grep` tool and `collapse_fires` stayed at zero.

So the surface is delivered per harness, but through one shared mechanism — the
`ctx hook` PreToolUse path that every host runs:

| harness | how search reaches the model | how the gap is closed |
|---|---|---|
| **Claude Code** | distinct `Grep`/`Glob` tools *and* Bash | `ctx wrap` sets `--disallowedTools Grep Glob` under collapse (tool absent, no wasted turn); the shared hook denies-and-redirects as a backstop |
| **Codex** | shell tool (ripgrep/grep in a command) | already covered — the shell command hits the Bash substitution path |
| **Antigravity** | command tool + any native search | command search is substituted; a native search tool is denied-and-redirected by the shared hook |

The common rule, enforced once in `_classify_native_search`: **with collapse
on, a native search tool is denied and redirected** to the collapsed op (or to
Bash `grep`, which is auto-substituted). For Claude Code the wrap additionally
*removes* the tool so the deny never has to fire. This is the default posture
on every host; `guard.collapse = false` is the break-glass off-switch.

## Measurement

The `sj` benchmark arm (`evals/bench_run.py`) *is* the collapsed product —
`ctx wrap` removes native search by default — so no separate arm is needed. The
row carries `collapse_fires` (loop-shapes collapsed, by shape) so adoption is
measured directly, the numerator the old vocab metric missed.

The first measured run (`evals/replacement-surface-2026-07-22.md`) is an honest
**N=1 null**: `collapse_fires` was 1 across six cells, adoption was flat, and
the one success flip was variance. What it *did* establish: the surface is
correct, safe, and cache-neutral (95% hit, undisturbed). The claim still to be
settled — with N≥5 repeats and loop-dominated tasks on a real repo — is whether
a fired substitution moves cost/success at all, or whether the engine, not just
the delivery, is the limit.
