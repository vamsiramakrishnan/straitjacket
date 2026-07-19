<sub><a href="README.md">« straitjacket / docs</a></sub>

# Digest closure: which operators compute on the compressed form

**Date:** 2026-07-19 · design law for the composition algebra. Companion to
[ALGEBRA.md](ALGEBRA.md) (which *builds* the `ctx q` algebra) and
[EVIDENCE-PLANS.md](EVIDENCE-PLANS.md) (which *compiles* pipelines). This doc
answers one question: **when can an operator run without rehydrating raw
bytes?** — and shows the answer is already enforced by the type system.

## The frame: rate–distortion, not compression

A digest is not a smaller copy of the raw output; it is a **rate–distortion
code**. Raw output `X` is compressed to a bounded representation `T̃` that
preserves the task-relevant variable `Y` while discarding the nuisance
dimension. The address (`run:<id>#stdout --lines A:B`, `--span`, `--symbol`)
is what makes the code **lazy-lossless**: the residual `X│T̃` is not
discarded, it is stored addressably, so distortion is *recoverable at a
queryable price* rather than lost. That is a **successive-refinement code** —
a low-rate always-on layer (the digest, resent every turn) plus a high-rate
on-demand layer (`ctx get`, paid only when the posterior over `Y` says it is
worth it).

An operator is **digest-closed** (a homomorphism over the representation) when
it computes its output from `T̃` alone — at digest-rate, never touching `X`.
If every operator in a pipeline is closed, the whole pipeline runs at
digest-rate end-to-end; the first non-closed operator is a **priced
refinement boundary** where bytes enter. Closure is the property that makes a
composition a real *algebra* rather than mere chaining.

## The theorem: closure is a function of the type signature

The `ctx q` algebra ([`src/ctx/query.py`](../src/ctx/query.py)) types every
stage `(input_kinds, output_kind)` over a fixed lattice of stream kinds
(`query.py`):

```
KINDS               = symbols · sites · files · records · text
REPRESENTATION_KINDS = symbols · sites · files · records     # bounded, digest-rate
TERMINAL_KIND        = text                                  # carries raw byte payload
```

Closure is then **derived from the signature alone** — no per-stage tagging
(`Stage.closure`, `query.py`):

| class | rule | meaning |
|---|---|---|
| **source** | `input_kinds == ()` | opens a pipeline: lifts the fact store / repo into the `sites` representation |
| **materialize** | `output_kind == text` | emits the terminal byte payload — the single priced refinement boundary |
| **closed** | otherwise | representation → representation, computed at digest-rate |

**Single-refinement-boundary theorem.** *In any `ctx q` pipeline, raw bytes
are materialized at most once, and only terminally.*

*Proof (structural).* The only stages that materialize bytes emit `text`
(design law, below). The materializer inputs are exactly `{sites, files}`
(`get ← sites`, `outline ← files`). No stage produces `sites` or `files` from
`text`: `sites`/`files` are produced only by source stages (which consume no
stream) and by `files` (`sites → files`) — never from `text`. Therefore once
a pipeline reaches `text` it can only reduce (`count → records`) or reorder
(`group`/`top`/`where`), never return to a materializer input. ∎

This is pinned as an executable invariant in
[`tests/test_digest_closure.py`](../tests/test_digest_closure.py):
`test_single_refinement_boundary_theorem` recomputes the materializer inputs
from the live registry and asserts no stage maps `text` back into one.
`pipeline_closure()` reports the verdict for any pipeline:

```
refs Foo | files                 → closed              (fully digest-rate)
search TimeoutError | group file | top 3 | count → closed
refs Foo | files | outline       → refinement@3:outline
search TimeoutError | get        → refinement@2:get
```

## The audit: every operator, classified

Grounded in the implementation — where a row says *materialize* it names the
line that rehydrates bytes.

### `ctx q` stages (the closed algebra)

| stage | signature | class | rehydrates? |
|---|---|---|---|
| `refs` `callers` `callees` `impact` | `() → sites` | source | fact store / call graph ([`callgraph.py`](../src/ctx/callgraph.py), [`codeverbs.py`](../src/ctx/codeverbs.py)) — bounded index, not the flood |
| `search` | `() → sites` | source | greps the repo but emits only coordinates ([`query.py` `_stage_search`](../src/ctx/query.py)) |
| `decls` `fails` | `() → sites/symbols` | source | typed facts (`facts.sqlite`) |
| `files` | `sites → files` | closed | pure record dedup — no store access |
| `group` `top` `where` | `KINDS → same` | closed | pure relational transforms on the stream |
| `count` | `KINDS → records` | closed | reducer to a scalar record (a dead-end for materialization) |
| `outline` | `files → text` | **materialize** | reads the tree-sitter **skeleton** (a bounded *derived* digest, cached per content hash — [`skeleton.py`](../src/ctx/skeleton.py)); terminal |
| `get` | `sites → text` | **materialize** | `_stage_get` calls `retrieval.get(..., Selector(lines))`, reading source bytes ([`query.py`](../src/ctx/query.py)); the refinement boundary |

Every stage but `get`/`outline` is closed, and both boundaries are terminal —
so the algebra is closed **by construction**, not by convention.

### Top-level verbs (outside the algebra)

| verb | class | note |
|---|---|---|
| `stats` | closed | shape (line/byte/token counts, record framing) already lives in the digest header — no recompute |
| `map` | source | ranks on import/reference **edges** (fact store), not file contents |
| `def` `refs` `diag` `callers` `callees` `impact` | source | code-graph / index queries → bounded coordinate output |
| `get` `search` | as above | the retrieval boundary |
| `diff run:A run:B` | **materialize → closable** | today rehydrates both artifacts (`get_blob`) then mines templates (`mine_templates`) in [`rundiff.py`](../src/ctx/rundiff.py). The output it wants — a *template-histogram delta* — is closed if the histogram is cached in each digest at capture time. **Prime candidate to fold into the closed algebra.** |
| `run` `seq` `eval` | producer | birth-gate capture: `X → T̃`. Not "closed" (they create the representation) but they never place `X` in context |

## The design law (for contributors)

1. **Byte materialization must emit the terminal kind (`text`).** A stage that
   reads raw artifact bytes may not return a representation kind — that would
   open a second door to byte-rate and break the theorem. Enforced by
   `test_design_law_materializers_emit_terminal_kind`.
2. **Prefer closing an operator over materializing.** Before a new verb reads
   raw bytes, ask *is its output a function of the digest?* If the needed
   answer is a histogram delta, a count, a set of coordinates, or a structural
   diff, it is closed — compute it on `T̃`. `diff` is the standing example of
   a verb whose closure is one cached histogram away.
3. **Price the boundary.** Where an operator genuinely needs residual bytes,
   it is a refinement query: surface the token price at the call site
   ([PRICED-CONTEXT.md](PRICED-CONTEXT.md)) and let the posterior over `Y`
   decide. `pipeline_closure()` names the boundary so a plan can price the
   closed prefix at digest-rate and only the suffix at byte-rate.

The payoff: a `ctx plan` / `ctx q` pipeline that stays in the representation
kinds runs entirely at digest-rate — O(hypothesis epochs) model rounds instead
of O(operations), with byte-rate paid once, at the end, only if the task
still needs it.
