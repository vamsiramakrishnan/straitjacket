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
