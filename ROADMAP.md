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

## M-F · Session-history learning loop (shipped v0.24, extending)

*The goldmine already on disk: every Claude Code host journals full
transcripts under `~/.claude/projects`. Replay them through the real
harness code, open-loop, and let real sessions rank the next mechanism.*

**Shipped**: `ctx replay [paths|--all-projects] [--gaps]` (`ctx.replay`) —
workspace-free, read-only, deterministic. Per session: interception
verdicts over recorded commands, recorded-vs-simulated wire residency,
and evidence sufficiency (facts the model provably used downstream,
scored inline-in-digest vs one-hop). `--gaps` aggregates the empirical
coverage priority list: raw tokens by claiming profile, slicer-heavy
programs, `ctx eval` opportunities. Read results are counted under the
read path, never shape-digested (a file containing test markers is not a
test run).

**Next increments**
- Regression gate in CI: replay archived harnessed transcripts after any
  profile change; evidence-sufficiency on digests a model actually worked
  from must not drop (measured 11/11 and 42/42 on spec3 archives).
- Divergence probes (small-model tier): at flagged starvation points,
  ask a canary model for its next action given the simulated digest;
  agreement is a canary metric, never proof — open-loop replay stops
  being ground truth at the first changed observation.
- Corpus intake: `ctx replay --gaps` output as a committed artifact per
  epoch, feeding the policy learner the same way wire telemetry does.

**Acceptance**: byte-deterministic reports given identical transcripts;
zero writes outside the throwaway store; redaction applied to every
printed fragment.

## M-J · Compiled evidence plans (`ctx plan` / `ctx investigate`)

**Shipped v0.25.0** (P0–P3 + P5 surface; P4 cost-table epoch = debt e319eef641; live four-arm referee open).

*Collapse every deterministic fan-out within a hypothesis epoch into one
locally executed, typed, bounded DAG — one model round in, one
decision-organized digest out.*

The model compiles its exploration intent into a total evidence plan
(`ctx.plan/v1`): typed ops (`ast.search`, `code.refs`, `test.run`,
`evidence.join`, `semantic.taint`), static validation and pricing, cost-based
physical engine selection (ast-grep / Semgrep / SCIP / facts / rg behind
logical ops), parallel execution with per-node CAS artifacts, and one
`investigate/v1` digest through the shipped EDC resolver. Interactive rounds
go from O(operations) to O(hypothesis epochs); replans are budgeted (default
1) and cache-resumed. Full design, phases P0–P5, and the frozen four-arm
referee: [`docs/EVIDENCE-PLANS.md`](docs/EVIDENCE-PLANS.md).

**Acceptance**: validator totality (typed rejections, cycle/budget/capability
checks); byte-identical investigation artifacts across replays; every claim
in the digest resolves via `ctx get`; observe-only plans on the MCP tier;
referee gates C ≥ B on turns at no correctness loss.

**Effort**: ~2 weeks across six gated phases. **Depends on**: EDC resolver +
contracts (shipped), `ctx q` stage registry (shipped), facts store joins
(shipped); ast-grep/Semgrep are opportunistic tiers, never required.

## M-K · The substrate operator classes (file sets · spans · records · rewrite breadth)

**K1–K3 + K5.3 shipped v0.26.0** (same day; 948-test suite green, live-
verified on this repository). Remaining: K4 SCIP, K5 comby behind its
gate, K3's optional jq engine + opportunity ledger, K2's scoped-scan
referee, K6 behind the broker.

**Designed 2026-07-20**, from the external "evidence algebra" proposal —
audited, corrected, and phased in [`docs/SUBSTRATE.md`](docs/SUBSTRATE.md).
The audit's verdict: the proposal's principle (integrate operator classes,
not binaries) is already M-J's shipped doctrine, and three of its six
additions already exist (`rg --json`, `ctags --json`, transactional
generation-guarded rewrites). What survives, in leverage order:

- **M-K1** span-precise sites: capture the rg submatch columns already on
  the wire; per-result search provenance. (~½ day)
- **M-K2** `corpus` / `repo.files`: the missing file-set operator class —
  bounded, receipted eligible-file sets that scan-class ops (`ast.search`,
  `semantic.*`) scope to via capped `foreach`; engines git ls-files → fd →
  os.walk; `--changed` from generation facts, never mtime. (~1 day)
- **M-K3** `records` source + `distinct`/`histogram` stages: the jq class
  absorbed as physical engine and instrumented escape hatch, never as
  bounded-tier vocabulary. (1–2 days)
- **M-K4** SCIP ingestion (M-G increment, resequenced above rewrites).
- **M-K5** comby as a second rewrite rung — if and only if a committed
  decline-corpus gate shows the population; explicit sed/awk steering
  ships independently.
- **M-K6** watch-based warming: deferred to the broker era (M-E) on the
  record; content-keyed laziness is the incremental algebra until then.

**Acceptance**: per-phase gates and named referees in the design doc;
engine parity byte-identical with kill-switches, coverage receipts on
every new emission kind, totality preserved (`test_digest_closure`).

## M-L · `ctx ask` — intents as typed plan presets (retrieval, decision-cost)

**Phase 0 + core intents shipped v0.27.0** — the adopted core of an
external `ctx ask` retrieval proposal, audited and resequenced in
[`docs/ASK.md`](docs/ASK.md). Compile a repository question into a frozen
`ctx.plan/v1` template with typed slots, execute on the shipped plan
executor, answer with the investigate digest. Collapses the *decision
cost* of exploration (which verbs, in what order) the way M-J collapsed
its *turn cost*.

- **Phase 0 · thin ops**: `evidence.failures` (failure census from
  captured facts — never a rerun; freshness vs the current generation
  declared), `code.symbols` (structured rows, census-before-detail),
  `code.context` (terminal bounded materialization — the closure
  boundary at the plan tier).
- **Phase 1 · intents + `ctx ask`**: `locate`, `impact`, `diagnose` as
  deterministic slot→plan presets. NO natural-language parser — intent is
  a flag, subject is a flag or the one unambiguous identifier token
  (disclosed); a missing/ambiguous slot is a teaching error that
  *suggests* and never guesses-and-runs. Every intent observe-class;
  counterevidence structural; bytes materialize once, terminally.

**Cut from the proposal** (recorded in the doc): the NL parser as primary
path, `reveal`/`audit` verbs, the whole-surface rebrand, the
entity/relation/operation ontology, and speculative `view:` projections.
**Deferred**: verify/review (execute-class), trace/compare, NL as sugar
over presets, role projections, shadow prefetch — each behind the per-
intent A/B/C referee (retrieval turns ≤ 50% of `ctx q`/`get` at no
recall loss).

## Sequencing

```
now ──► M-A quarantine template ─┐
        M-D run-diff ────────────┼─► eval refresh (exploration + regression tasks)
        M-C map v1 (ast/ctags) ──┘
next ─► M-B code verbs v1 (jedi) ──► MCP op growth, skill update
then ─► M-E broker ──► M-B multi-language LSP · M-C cached indexes
        + learned policy epochs (telemetry → committed policy, existing plan)
now ──► M-J compiled evidence plans (shipped v0.25.0; P4 + referee open)
now ──► M-K substrate operators: K1 spans ✅ K2 corpus ✅ K3 records ✅
        K5.3 sed/awk steering ✅ (v0.26.0) · next: K4 SCIP; K5 comby
        behind its decline-corpus gate; K6 waits for M-E
now ──► M-L ctx ask: Phase 0 thin ops ✅ Phase 1 locate/impact/diagnose ✅
        Phase 2 trace/compare ✅ Phase 3 verify/review ✅ (v0.29.0)
        · next: A/B/C payoff referee per intent (flood task); NL sugar
now ──► M-K tail: K3 records_opportunity ledger ✅ · K5 comby decline-gate
        instrumented ✅ (rung still gated) · K2 scoped-scan receipt ✅
        (95% file cut, 13× ast-grep) · K4 SCIP ingestion ✅ (v0.30.0:
        precise xrefs, 100% vs 50% precision on the ambiguity fixture;
        tree-sitter grammar-wheel backend shipped too)
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

Shipped v0.8.0, the measurement-loop wave: the prefix-stability contract
(injected prefix bytes are golden-hashed; changing them is a versioned
decision), the session scorecard (wire ground truth → cache economics,
timing split, effort mix, per-session history for the policy learner),
graduated engagement (affordance surface scales with measured task scale;
lean-model profiles), the emission governor (post-tool-use nudges when
proxy-measured output volume crosses tiers — the symmetric partner of the
read budget), and anticipatory inlining (the first pytest failure region
rides the digest, saving a retrieval hop). Each traces to a measured
failure in evals/matrix-2026-07-18.md.

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
