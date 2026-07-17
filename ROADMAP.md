# Roadmap: elegant mechanisms

Every mechanism here follows the house rule: **replace bytes with addresses.**
Each entry states its contract, its dependencies, and the acceptance gate it
must pass before it ships. Determinism, budgets, declared omission, and
telemetry are inherited invariants — new verbs get them for free or they
don't merge.

## M-A · Provenance-bearing sub-agent quarantine

*The mechanism to steal from Claude Code's Task tool; the flaw to fix is
conclusions without evidence.*

Exploration burns context. Forked sub-agents solve that — the parent sees
only the conclusion — but the conclusion arrives unauditable. Under the
harness, both halves compose: the fork's tool calls flow through the same
PreToolUse steering (settings inherit), so its evidence lands in the shared
artifact store; its report cites handles the parent can spot-check.

**Deliverables**
- `plugins/*/agents/ctx-explorer.md` — an agent definition shipped by the
  plugin and installed by `ctx wrap`: instructs the sub-agent to gather
  evidence via ctx verbs and to report in the checkpoint shape (conclusion,
  evidence handles with coordinates, searches attempted including negative
  ones). The report schema **is** `ctx.checkpoint/v1` — no new format.
- Wrap/installer wiring for both hosts; docs section "auditable delegation".
- Eval: an exploration-heavy task (e.g. "map how budgets flow through this
  codebase") measuring parent-context tokens and evidence verifiability,
  quarantined-with-handles vs inline.

**Acceptance**: parent transcript contains only the report; every claim in
the report resolves via `ctx get` on a cited handle; fork artifacts survive
in the store with leases.

**Effort**: ~½ day. **Depends on**: nothing — all plumbing exists.

## M-B · Symbol-addressed code verbs (Serena-validated LSP tier)

*Meaning-indexed access replaces byte dumps: read one body, cite one span.*

**Deliverables**
- `ctx def <repo:file[:symbol]>` — definition site, snapshot-on-read, span
  token attached.
- `ctx refs <symbol>` — reference sites as coordinates (file:line, sorted,
  budget-capped with continuation), each snapshot-backed.
- `ctx diag [repo:path]` — type/lint diagnostics as a deterministic digest.
- v1 backend: **jedi** (pure-Python, library-mode, no server lifecycle) as an
  optional extra `[code]`; graceful fallback to the existing ast `--symbol`
  machinery when absent. Multi-language LSP servers (pyright, tsserver,
  gopls, rust-analyzer) arrive in the broker era (M-E) where they can stay
  warm; the verb contracts do not change when the backend does.
- MCP: `op` enum grows `def | refs | diag`; single-tool surface preserved.

**Acceptance**: refs over this repository resolve correctly and
deterministically (sorted by path, line); every emitted site is
snapshot-backed; outputs respect `result_tokens`; absence of jedi degrades
to ast with a labeled note, never an error.

**Effort**: 1–2 days. **Depends on**: nothing hard; better under M-E.

## M-C · Ranked repo map (Aider-validated, budget-fixed)

*Global awareness at constant token cost; degrade gracefully, never explode.*

**Deliverables**
- `ctx map [--budget N] [--focus <path|query>]` — a deterministic codebase
  map fitted to a token budget: files ranked by a reference-graph score
  (imports + symbol usage; fixed iteration count, deterministic tie-breaks),
  top symbols per file with signatures, each addressable via
  `repo:file --symbol X`.
- Python analysis via stdlib ast; other languages via **universal-ctags when
  on PATH** (the ripgrep pattern: opportunistic binary, transparent
  fallback to tree + language inventory from `ctx stats`).
- Cache keyed by worktree hash — identical tree, identical map, no recompute.

**Acceptance**: byte-identical output for an unchanged worktree; hard budget
compliance at 200/500/1200 tokens; focus re-ranks without breaking
determinism; map entries resolve via existing selectors.

**Effort**: 1–2 days.

## M-D · Run-to-run regression digests

*The comparative question every debugging session actually asks.*

**Deliverables**
- `ctx diff run:A run:B` — deterministic delta digest: exit/signal changes,
  failure-signature deltas (via test profiles), template deltas (via
  logtemplate mining: templates appearing/disappearing/changing counts),
  stream size deltas. Spans minted for new-in-B evidence.

**Acceptance**: passing→failing pytest runs surface the new failure with
coordinates in ≤ digest budget; identical runs produce "no behavioral delta"
plus size accounting; byte-determinism given the same pair.

**Effort**: ~1 day. **Depends on**: nothing — profiles and spans exist.

## M-E · Broker era (existing Phase 3, unchanged but re-motivated)

The resident broker remains scheduled for the security boundary (separate OS
identity, capability HMAC handles, encrypted blobs). This roadmap adds two
tenants it hosts for free: warm LSP servers for M-B's multi-language tier,
and cached map/graph indexes for M-C. Latency wins are incidental, not the
justification (see the language analysis in evals/).

## Sequencing

```
now ──► M-A quarantine template ─┐
        M-D run-diff ────────────┼─► eval refresh (exploration + regression tasks)
        M-C map v1 (ast/ctags) ──┘
next ─► M-B code verbs v1 (jedi) ──► MCP op growth, skill update
then ─► M-E broker ──► M-B multi-language LSP · M-C cached indexes
        + learned policy epochs (telemetry → committed policy, existing plan)
```

Deliberately **not** planned: wire-side semantic compression (LLMLingua-class
token pruning) and provider-side compaction reliance — both delete bytes
without addresses, which the needle-drop eval shows is the failure mode this
project exists to prevent.

---

# The unified architecture: absorbing the full taxonomy

The taxonomy's quadrants are the four moments of a token's lifecycle. One
artifact store serves all four as *gates*; each mechanism's elegant core maps
to a gate, and its documented limit is repaired by the shared contract
(deterministic views · resolvable addresses · declared omission · leases).

## Gate 1 — Birth (source-side; shipped, extending)

| Absorbed from | As | Their limit, repaired |
|---|---|---|
| Serena | M-B symbol verbs (`def/refs/diag`) | no provenance → every site snapshot-backed + span-tagged |
| Aider repo map | M-C `ctx map --budget` | static view → **evidence-weighted ranking**: files implicated in recent failing runs (we hold the artifacts) rank above cold graph score |
| Graphify | M-C index cached by worktree hash (broker-era) | staleness → content-keyed cache; uncitable answers → answers resolve to snapshots |
| Deferred tools / skills JIT | already practiced (single MCP tool; teaching via `next:` lines) | — |

## Gate 2 — Entry (wire-side, done right)

The only wire-side idea worth keeping is *coverage*: content from channels we
don't govern (foreign MCP tools, pasted blobs). Build: the **adopt tier** —
where the host exposes tool-result interception, oversized foreign results
are artifactized once at first entry (digest + handle), never rewritten
per-request. LLMLingua's informativeness scoring is absorbed as a
**deterministic salience stage** in profiles (error-first, rare-first —
versioned scoring chooses what to *show*, never what to *keep*). Middle-out's
position insight becomes digest layout policy (load-bearing evidence at the
edges) and is mostly mooted by keeping digests short. Model cascades compose
for free and become *safer* under the harness: handles are the interchange
format — a cheap-model sub-agent's citations resolve identically for the
strong parent, so misroutes are auditable instead of fatal.

## Gate 3 — Residence (lifecycle-side)

| Absorbed from | As |
|---|---|
| Sub-agent quarantine | M-A explorer agents reporting in checkpoint shape with handle citations |
| MemGPT paging | the **evidence controller**: turn ledger + budgets as the paging policy; the store is the backing memory, `ctx get` is the page fault, live handles are the page table — paging without interrupts because references are cheap |
| Filesystem-as-memory | artifact equivalence: adopt-on-first-read of agent-created files; the checkpoint doc is the principled recitation object |
| Server-side compaction / context editing | **checkpoint-then-compact protocol**: secure evidence into handles *first*, then provider-side clearing/compaction is free and lossless — birth-time capture is what makes context editing safe rather than destructive |

## Gate 4 — Emission (output-side)

Cite-don't-quote and checkpoint-shaped sub-agent reports (shipped, skill
rules 11–12) are the lossless form of Caveman. `ctx diff run:A run:B` (M-D)
is the runtime analog of diff-based editing. Prompt caching is the economic
proof of the whole design: append-only + byte-identical digests maximize the
provider subsidy — measured in the overhaul benchmark, where the per-request
rewriting proxy paid 3.6× our cache-write volume.

## Not absorbed, by principle

Lossy pruning without addresses (LLMLingua as-shipped, middle-out as-shipped,
compaction *without* a prior checkpoint), and style-lossy output compression
for human-facing text. Deleting bytes you cannot re-address is the one move
this architecture exists to make unnecessary.

## Out of scope, by deployment

KV-cache infra (attention sinks, H2O eviction, KV quantization): invisible
through managed LLM APIs. The only API-visible lever over provider KV state
is prompt caching, i.e. prefix stability — already maximized by the
append-only/byte-identical design. Revisit only if a self-hosted model
deployment materializes in the broker era.
