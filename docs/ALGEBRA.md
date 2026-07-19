<img src="../assets/readme/docs/algebra.svg" width="100%" alt="Facts and the composition algebra. Architecture wave, doc 4 of 4."/>

<sub><a href="README.md">« straitjacket / docs</a></sub>

# Facts and the composition algebra

**Date:** 2026-07-19 · design for the post-EDC wave. Four questions
(tree-sitter indexing · Glean/modern indexers · a compositional algebra
more token-efficient than `ctx eval` · Angle-inspired mechanisms) that
converge on one architecture: **the EDC governs how evidence is
delivered; this layer governs how evidence is derived and composed.**

## M-F · Tree-sitter skeleton tier (the Maki index, with addresses)

Maki parses 15 languages into skeletons — imports, types, signatures with
line numbers — and reads the skeleton by default. Ours, house-styled:

- Optional `[code]` extra (py-tree-sitter + language pack), fallback
  chain unchanged in contract: tree-sitter → ctags → stdlib ast (py) →
  tree + inventory. Absence degrades, never errors.
- Skeletons are **derived artifacts**: content-addressed canonical-JSON
  blobs keyed by the source file's blob hash (parse once per content,
  ever — the FLOOD-census pattern generalized). Each symbol row carries
  name, kind, signature, line range, and a minted span.
- Consumers: `ctx stats` priced outline goes multi-language; `ctx map`
  ranks on real import/reference edges beyond Python; skeleton-first
  steered reads for oversized code files.
- **The unification**: reading a file is a command family with an
  Evidence Contract — REQUIRED: every top-level symbol identity +
  signature + range (the census); ELASTIC: docstrings/leading comments;
  RETRIEVABLE: bodies via spans. Census-before-detail applied to source.
  Tree-sitter is just this family's extractor; delivery rides the same
  resolver/plans/reflex as pytest. No new policy machinery.

## M-G · The fact store (Glean's idea, not Glean's infra)

Glean's insight: code knowledge as **typed facts** in a queryable store,
written by many indexers, queried declaratively (Angle). Glean itself is
server infrastructure with per-language indexer fleets — wrong weight
class for a stdlib-first harness. Adopt the idea:

- `facts.sqlite` in the workspace store: typed predicates as rows —
  `decl(symbol, kind, file, range, span)`, `imports(file, module)`,
  `ref(symbol, file, line)` — derived from M-F skeletons; plus
  **opportunistic ingestion adapters** for modern interchange formats
  when present: **SCIP** first (single-binary indexers, protobuf index —
  `scip-python`/`scip-typescript`/`scip-java` cover precise xrefs without
  running LSP servers), LSIF read-only second. The ripgrep pattern:
  binaries on PATH are used, absence costs nothing.
- **The asset nobody else has**: our facts are not only static. Evidence
  graphs are facts (`fail(test, failure_class, frame_file, frame_line)`),
  generations are facts (`changed(file, gen)`), outcomes are facts.
  Glean/Kythe/SCIP know what code *is*; the store also knows what code
  *did*, when, and what the reader did about it — static × dynamic ×
  temporal, content-addressed.

## M-H · `ctx q`: the composition algebra (token-efficient, TOTAL)

`ctx eval` is Turing-complete Python: ~150–300 tokens of model output,
quoting hazards, and a trust envelope that keeps it off the MCP tier.
Most real compositions don't need Turing-completeness — they need
pipeline algebra over bounded verbs:

    ctx q 'refs TokenBucket | group file | top 3 | get --context 5'
    ctx q 'fails run:abc | frames | changed gen:11 | outline'

- **Combinators, not code**: verbs as stages over typed record streams
  (symbols, sites, failures, files); `group`, `top`, `where`, `count`,
  `join` as the only control constructs. ~20–40 tokens of model output
  for what costs eval ~200 — the compression is of *intent*, attacking
  the expensive output-token stream (the Ponytail lesson, mechanized).
- **Total by construction**: no loops, no recursion, bounded stages —
  every query terminates, costs are statically boundable, and therefore
  the algebra is **safe for the bounded MCP tier**, which arbitrary-code
  eval can never be. This closes the voluntary-tier capability gap: the
  TokenSave-shaped surface gains real composition without gaining code
  execution.
- **Per-stage provenance for free**: unlike eval (whose intermediates
  live only inside the script), each stage's result set is store-backed
  and addressable — `seq`'s per-step handles, generalized. The final
  emission rides the EDC: contract-checked, plan-rendered, bounded.
- eval remains for genuinely computed control flow; the algebra covers
  the measured 80% (the eval-collapse scenarios S-A and S-B are both
  expressible as one-line queries).

## M-I · Angle-lite: the joins that debug for you

Angle's power is declarative joins over facts with derived predicates.
Angle-lite = conjunctive queries + bounded transitive closure (depth-
capped, like `ctx impact`) over M-G's store — no full Datalog engine.
The queries that matter join planes no other system holds together:

    fail(T, _, F, L), decl(S, _, F, R), within(L, R), changed(F, gen)
      → "failing frames inside symbols changed THIS generation"
      — the root-cause query, one bounded digest.

    fail(T1, C, F, _), fail(T2, C, F, _), T1 != T2
      → shared-cause grouping as a QUERY (EDC §12.3's deterministic
        group labels, derivable instead of renderer-coded).

    ref(S, F, _), not changed(F, _), fail(_, _, F, _)
      → "failures in code nobody touched" — flake/suspect triage.

Emission is EDC-governed like everything else: a query result is an
evidence graph with a generic contract (rows REQUIRED as census, detail
retrievable). Determinism: fact derivation is content-keyed; queries are
pure; results carry the fact-store epoch they ran against.

## Sequencing and gates

1. **M-F** first (standalone value; unblocks polyglot outlines; the
   file-read contract proves the "everything is a family" claim).
2. **M-H** second (highest token leverage; referee: re-run the
   eval-collapse mechanical scenarios with a `q` arm — target: eval-arm
   correctness at ≤1/4 the model-authored tokens; plus MCP-tier parity
   tests).
3. **M-G/M-I** third (facts derived from M-F skeletons + existing
   evidence graphs; SCIP adapter opportunistic; the root-cause join gets
   its own eval: seeded multi-failure fixture, query answers in one
   digest what round-1 haiku burned 8 turns discovering).
4. Non-goals inherited from EDC §22 plus: no server daemons before the
   broker era (M-E); no language-specific indexer fleets (SCIP ingestion
   over bespoke indexers); tree-sitter never parses tool output
   (evidence extractors own that plane).

## The atlas: every plane, one picture

Legend: ✅ shipped (v0.22.0, gates passed) · ⊙ shipped in shadow ·
○ designed (this doc / EDC), referee named, not built.

```
╔══════════════════ SAFETY PLANE — never adaptive, spans both ends ══════════════════╗
║ entry: authz · path confinement · secret-path deny · hard caps · timeouts · killpg ║
║ exit:  redaction · ANSI/control strip at emission                                  ║
║ guard classes: safety ⇒ adaptive:false — property-tested byte-identical under      ║
║ every reflex/circuit/epoch state ✅                                                 ║
╚═══════════════════════════════════╦════════════════════════════════════════════════╝
                                    ║ (everything below runs inside it)
 DERIVATION PLANE — what code IS    ║          EXECUTION PLANE — what code DOES ✅
 ┌────────────┐ ○ tree-sitter .scm  ║  model ──► hook PreToolUse ✅ ──────────────┐
 │ repo files │──(error-tolerant,──►║    │   steer│teach│allow · adoption ledger  │
 └─────┬──────┘   15+ langs)        ║    │   steer-shadow(6b) ⊙ · Edit/Write──►gen│
       │      ┌──────────────────┐  ║    ▼                                        │
       │      │ skeleton blobs   │  ║  birth gate: run│seq│eval│job(--bg) ✅       │
       │      │ (derived CAS,    │  ║    │ raw bytes, ALL of them                 │
       │      │ keyed by file    │  ║    ▼                                        │
       │      │ blob hash — no   │  ║  ┌─────────────────────────────┐           │
       │      │ invalidation)    │  ║  │ ARTIFACT STORE ✅ CAS:      │           │
       │      └───────┬──────────┘  ║  │ blobs·manifests·spans·leases │           │
       │ ⊙ scip-*     ▼             ║  │ + derived blobs (census ✅,  │           │
       │ index.scip ┌───────────┐   ║  │   skeletons ○, facts ○)     │           │
       └──ingest───►│ facts.db ○│   ║  └──────────┬──────────────────┘           │
                    │ decl·ref· │   ║             ▼                               │
   ○ Angle-lite ───►│ import·   │   ║  EVIDENCE PLANE — what the model NEEDS ✅   │
   bounded joins:   │ fail·     │◄──╬── extractors: pytest/v2 ✅ lint go jest     │
   fail⋈decl⋈       │ changed(g)│   ║   build json log text (contract tables)     │
   changed(gen) =   │ + spans   │   ║             │ EvidenceGraph: items·coverage·│
   root-cause query └───────────┘   ║             │ attested·volatile-quarantined │
                                    ║             ▼                               │
   ○ ctx q 'refs X | group file     ║   Evidence Contract (TOML) ✅               │
     | top 3 | get --context 5'     ║   REQUIRED/ELASTIC/RETRIEVABLE · loss table │
   total algebra: no loops ⇒        ║   floor≤ceiling · validate_selection at the │
   terminates ⇒ MCP-tier safe;      ║   SEAM (typed facts, never re-parsed text)  │
   per-stage provenance; 20-40 tok  ║             ▼                               │
   of intent vs eval's ~200         ║   Delivery Policy Resolver ✅ (ONE choke    │
                                    ║   point; 7 hand-rolled sites retired)       │
        inputs ─────────────────────╬──► contract · SessionState(reflex) ·        │
                                    ║   Signals(window%·tier) · ReaderCapability  │
                                    ║   (latched posterior) · Policy Epoch        │
                                    ║             ▼                               │
                                    ║   DeliveryPlan ✅ plan_id·closed reasons    │
                                    ║   PASS_SUMMARY│FAIL_CENSUS│DENSE│FLOOD│     │
                                    ║   BYPASS(two entries: 6b null ○ · breaker ⊙)│
                                    ║             ▼                               │
                                    ║   pure Renderer ✅ → RenderedEvidence       │
                                    ║   (text · CoverageReceipt · plan)           │
                                    ║   Rule 14: same graph+contract+plan ⇒       │
                                    ║   identical bytes (property-tested)         │
                                    ║      │                    │                 │
                                    ║      ▼                    ▼                 │
                                    ║  transcript ✅        store (census blob:,  │
                                    ║  bounded · addressed  spans) ✅             │
                                    ║  declared omission                          │
                                    ║      │                                      │
 BEHAVIOR LOOP — what the model DID ║      ▼                                      │
  model's next action ◄─────────────╬──────┘                                      │
      │                             ║                                             │
      ▼                             ║   generations §8 ✅⊙: sig-relation ×        │
  outcome tracker ⊙ landing·        ║   gen-equality (porcelain + untracked       │
  narrowed·validated-after-edit·    ║   (path,size,mtime), ledger-dir excluded)   │
  equiv-rerun·slicer·workaround·    ║   confirmed-by-content ⊙ beats event guess  │
  expired(CENSORED — never trains   ║◄────────────────────────────────────────────┘
  pessimism)                        ║
      │ intervention events ⊙ (deterministic iid · coverage · hypotheses w/ windows)
      ├── fast loop: reflex ✅ / circuit ⊙ NORMAL→DENSE→BYPASS
      │   episode = signature × generation · one transition per level per episode
      │   hysteresis: earned recovery (2/3 positives) · replay gates vs archived
      │   transcripts: ALL PASS ✅
      └── slow loop: scorecard v2 ✅ (anomalies · coverage tables · episode
          narratives · counterfactuals formula-labeled) ──► epoch compiler ✅
          [digest_density] reviewed·committed ──► Resolver defaults, next session ↺

 REFEREE ✅ (outside everything, judges all of it): spec3 --repeats N --gates ·
 frozen-constants checksum · medians · §19.2 thresholds frozen pre-build ·
 v0.22.0 verdict: ALL GATES PASS (tokenbucket 1.2× naive at cost parity)
```
