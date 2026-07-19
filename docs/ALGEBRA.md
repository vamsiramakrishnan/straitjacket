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
