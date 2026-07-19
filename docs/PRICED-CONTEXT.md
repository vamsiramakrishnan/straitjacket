<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/readme/docs/priced-context.svg">
  <img src="../assets/readme/docs/priced-context-light.svg" width="100%" alt="Priced Context — metadata as economic signposting. Mechanism thesis, shipped.">
</picture>

<sub><a href="README.md">« straitjacket / docs</a></sub>

# Priced Context: a thesis on metadata as economic signposting

**Claim.** An agent's retrieval choices are economically rational only to
the extent that every choice carries a visible price *at decision time*,
denominated in the currency the agent is budgeted in (tokens), relativized
to its remaining budget (window fullness), and attached to a cheaper
actionable alternative. Metadata that satisfies these four properties makes
the agent self-directing; metadata that violates any of them is either
noise or a new cost.

Existence proof from our own bench: the S3 comprehension cell
(evals/matrix-2026-07-18.md) was won *entirely* by priced structure —
`ctx map`/`get` cost ~600 tokens of metadata and produced the only correct
answer of four cells; the unpriced arms burned their full turn budgets
scrolling and returned nothing.

## Principles (each falsifiable, each tested below)

- **P1 — Pre-decision beats post-error.** A denial still costs a full
  round trip (~1.5–2s ttfb + one suffix cache write, per wire timing).
  The cheapest mistake is the one the menu prevented. Prices belong on
  the menu, not (only) on the rejection slip.
- **P2 — Tokens are the native currency; window % is the exchange rate.**
  "87 KB" forces mental conversion; "~22k tok ≈ 11% of window" is
  directly comparable against the value of reading it.
- **P3 — Precision only needs to cross decision thresholds.** The model
  acts identically on "~8k" and "8,432"; log-bucketed prices are strictly
  better per metadata byte and compress/cache better.
- **P4 — Structured-lossy beats truncated-lossy.** When a read must
  degrade, a priced symbol outline (every entry: name, lines, ~tokens,
  span handle) dominates "first 240 lines": same budget, but every line
  of the outline is an actionable next step.
- **P5 — Annotate survivors, not inventories.** Metadata is context
  spend. Selection first (ranking, or the object actually touched), then
  annotation. A flat priced inventory of the repo is itself token bloat.
- **P6 — Prices ride the suffix, never the prefix.** Priced listings are
  volatile; injecting them into the shared system prompt would violate
  the prefix-stability contract and cold-invalidate caches.
- **P7 — Menus are affordances.** Small models execute menus as to-do
  lists (measured: S1:haiku v0.7.1). Unsolicited price tags obey graduated
  engagement; explicitly requested outlines do not need filtering — the
  agent asked.

## Cheap-test results (local, no LLM, measured 2026-07-18)

- **T1 — Outline economics** (all 28 .py files in src/ctx): the priced ast
  outline (name, line range, ~tokens, span handle per symbol) is **24.7×
  cheaper on average** than the file it describes (median 23.5×, worst
  12.8×, best 54.5×); for the 5 files over the 16 KiB inline budget —
  exactly the ones the guard degrades — the ratio is 16.3–54.5×. → P4
  confirmed; outline-as-menu is the right degrade path for structured
  files.
- **T2 — Flat-inventory kill test:** annotating every file in this repo
  (156 files × ~7 tok) costs 1,092 standing tokens vs 210 for the top-30
  map survivors — 5× waste that scales linearly with repo size (a
  2,000-file repo pays ~14k tokens for the flat version). → P5 confirmed;
  the "priced repo inventory" idea is eliminated.
- **T3 — Price-tag cost at hook time:** size→bucket→window% measures
  **0.003 ms/call** against a ~0.3 ms warm classify (the stat and
  window.json read already happen). → P1/P2 are free to implement.
- **T4 — Data-file key preview** (all parseable .json in repo): key
  preview from a bounded 64 KiB head read runs 7–40× cheaper than the
  file (e.g. invocation schema: ~656 tok → ~20 tok preview, 33×). Real
  but smaller absolute stakes than T1 → shipped only where stats already
  parses the file, not as a new scanning pass.
- **T5 — Bucketing:** log-ish buckets (<1k, ~1k, ~2k, ~5k, ~10k, ~20k,
  ~50k, ~100k+) round-trip deterministically and never mislead across a
  decision boundary by construction (bucket edges sit at real behavioral
  thresholds: inline budget, fractions of the window).

## Mechanisms shipped from the surviving ideas (v0.9.0)

1. **Price tags in guard steering** — every oversized-read/unbounded
   deny/rewrite reason now carries "~⟨bucket⟩ tok (~P% of window)" computed
   from the stat the hook already performs and the window.json it already
   reads. Peak-receptivity placement: the exact moment of the expensive
   choice.
2. **Priced symbol outlines** — `ctx stats repo:<file>` on a code file
   returns the symbol menu: name, kind, line range, ~tokens each, span
   handle. The guard's oversized-read remediation names this verb with the
   file's price, converting a wall into a menu (P1+P4).
3. **Priced map survivors** — `ctx map` entries carry "~⟨tok⟩·⟨defs⟩d"
   for ranked survivors only (P5), from data the ranking pass already has.

Rejected, with reasons: flat priced inventories (T2), per-file prose
summaries (nondeterministic, stale, unpriceable), exact token counts (P3),
prefix-resident repo metadata (P6), unsolicited outlines pushed into
passive sessions (P7).
