# Evidence Delivery Controller (EDC)

**Status:** adopted target architecture (2026-07-19) for the digest layer —
the ten reflex design rules (docs/REFLEX.md) assembled into one system.
Builds via the pytest/v2 instance first (rule 10), judged by the n≥3
spec3 referee (debt 34e21fe2dc), then lifted to the shared layer.

## The inversion

Current pipeline (measured failing in evals/spec3-haiku-2026-07-18.md):

```
command result → profile renders its preferred summary → budget expands/truncates
```

Size is the objective; coverage is best-effort — and the `bounded()`
backstop will cut *required facts* as long as it declares the omission.
Declared starvation is still starvation.

EDC pipeline:

```
command result → retain complete artifacts (birth gate, shipped)
              → extract typed Facts (per command family)
              → apply the family's Evidence Contract
              → select a deterministic Delivery Plan
                  (contract · window/token constraints · reader-tier prior
                   · observed session behavior (reflex) · committed epoch)
              → render deterministically · record progress
              → densify / open the bounded compression circuit on starvation
```

> **Evidence sufficiency is the objective. Output size is a constraint.**

## What maps to what (refactor, not greenfield)

| EDC element | existing asset |
|---|---|
| complete artifacts | birth gate (`run_capture`, CAS store) — shipped |
| Facts extraction | pytest/lint censuses, logtemplate templates — embryonic, per-profile |
| Delivery Plan inputs | the LADDERS signal record: window.json, tier, reflex state, ctx-policy.toml |
| progress recording | failing-count trajectory (rule 9) — census makes it free |
| densify / circuit | reflex v2 (shipped) + breaker (planned, rule 6) |
| supersedes | LADDERS `resolve_budget` choke point — EDC plans *presentation*, budget is one input |

The genuinely new object is the **Evidence Contract**: it makes rules 1–2
machine-checkable. A plan that cannot fit REQUIRED facts escalates
(bigger budget → dense → breaker) — it never silently truncates them.

## The layering law

> **Policy cannot buy what extraction didn't build.** Budget, tier,
> reflex, and epoch policy select among representations extraction made
> available; no amount of downstream conditionality can recover a fact
> class the semantic model never represented. (Receipt: spec3 round 1 —
> failure asymmetry doubled the budget and the renderer spent it on the
> only structure it had, one traceback; seven failure identities were
> unreachable at any budget.)

The five layers are separated because each fails differently and is
fixed differently:

| layer | failure signature | fix path | in-session recoverable? |
|---|---|---|---|
| 1 · semantic extraction | fact class absent; budget increases change nothing | code (extractor) | ❌ — invisible to policy |
| 2 · evidence requirements (contract) | digest looks complete, decisions still starve — the *silent* failure | contract table review | ❌ — looks like model error |
| 3 · delivery policy | wrong plan for this reader/moment | reflex (fast), epoch (slow) | ✅ — the adaptive layer |
| 4 · rendering | facts present but illegible/mis-ordered | golden tests | ✅ trivially |
| 5 · outcome measurement | loop blind or mis-trained | ledger/schema audit | ❌ — corrupts the slow loop |

Conflating layers is what made an extraction defect masquerade as a
budget defect. Only layer 3 may adapt at runtime; layers 1–2 change by
code and committed tables; layer 5 is the instrument and must stay
boring. Test classes follow the layers: **contract-conformance tests**
(Facts completeness vs raw output — does extraction represent every
REQUIRED class?) are a distinct suite from rendering goldens, and the
absence of that suite is how pytest/v1 shipped structurally starved.

## Design refinements (binding)

1. **The null plan is first-class.** The plan space includes raw
   passthrough — for small outputs (today's zero-hop inline) and for the
   graduated-steering regime (debt c273b8d3d0). The measured lesson of
   spec3: sometimes the optimal intervention is none; a controller whose
   cheapest plan is ceremony rebuilds the small-task tax at a new layer.
2. **Contracts carry a summarization ladder, not a boolean REQUIRED.**
   Sufficiency degrades hierarchically under hard limits: enumerate ≤N →
   group-by-file counts + spans ≤M → distribution + top-k, every rung
   address-bearing (census-of-census). Without the ladder, REQUIRED
   either lies under pressure or floods.
3. **Fact lists before fact graphs.** Versioned flat records
   (id, coordinates, span, severity, family fields). Relations arrive
   when a consumer needs them; determinism and schema versioning are the
   cost centers, not expressiveness.
4. **Progress is family-specific.** pytest: failing-count delta on the
   next *equivalent* run (worktree-hash equivalence, rule 5 v3); lint:
   diagnostic delta; build: error delta. Generic "task progress" is
   unmeasurable; family deltas are exact, free from the census, and are
   the [digest_density] training signal.
5. **Presentation never enters content identity.** Facts extraction is
   versioned like profiles today; the Plan selects among deterministic
   renderings; digest identity remains a pure function of rendered bytes
   (the reflex-wave contract, kept).
6. **Safety is outside the plan space** (rule 7): redaction, secret-path
   denies, and containment are applied after rendering and are not
   selectable, degradable, or adaptable by any plan, reflex, or epoch.

## The objective function (§2, adopted with three amendments)

For a delivery plan *p*, minimize expected total downstream cost:

```
E[J(p)] = C_emit(p) · R(session)          ← residency-scaled, not one-shot
        + P_rerun(p) · C_rerun
        + P_retrieval(p) · C_retrieval
subject to: EvidenceContract(p) satisfied   ← sufficiency is a constraint,
                                              never a priced term
```

Amendments to the proposed form:

1. **Residency term** — emitted tokens are re-sent every subsequent
   request and occupy window (measured as `resend` in evalset_collapse).
   Without R(session), the objective justifies unlimited inlining and
   rebuilds the flood at the planner level.
2. **Sufficiency as hard constraint** — pricing P_miss·C_quality puts
   correctness on the market (rule 7's failure mode in economics
   clothing). The contract is the constraint; costs are minimized
   subject to it.
3. **The ledger is the estimator** — P_rerun per family×tier IS the
   starvation rate; P_retrieval IS the landing rate; both accumulate in
   reflex-outcomes.jsonl today. Policy tables are compiled, reviewed,
   conservative snapshots of these estimates (the epoch pipeline). No
   runtime ML, and no invented constants either.

**Why the taxonomy below is the practical form of this objective:** under
measured cost dominance (C_rerun ≈ a turn + execution + cache append +
state-loss risk ≫ ΔC_emit ≈ tens of tokens per census row), the argmin
collapses to a rule — batch-critical inline and complete, drill-down
behind addresses. The planner is a table lookup except at regime
boundaries (huge censuses, starved windows), where the summarization
ladder governs.

## The information taxonomy (§3, adopted)

- **Batch-critical structure**: the complete item set needed to decide
  the next batch of work (every failing test / lint diagnostic / type
  error / failed target / conflicting file / invalid record). Delivered
  inline and completely — omitting one item risks a tool call, a turn,
  a re-execution, a digest, a cache append, and a state-loss
  opportunity, each individually more expensive than the row.
- **Drill-down evidence**: detail needed after the model selects an item
  (full traceback, source context, complete stdout, one large diff).
  Content-addressed retrieval is the correct home.

**The pytest delivery hierarchy** (the plan space; densify = descent
depth, breaker = the floor, null plan = level 0):

| level | content | status v0.21 |
|---|---|---|
| 1 | outcome + counts | ✅ |
| 2 | complete failing-test census | ✅ |
| 3 | file, line, failure class per failing test | ⚠️ class missing — pytest/v2 |
| 4 | one-line failure summary per failing test | ⚠️ dense-only today — pytest/v2 makes it default |
| 5 | detailed evidence, root/first failure | ✅ |
| 6 | full traceback per failure behind stable addresses | ✅ (census spans) |
| 7 | teaching / retrieval prose | ✅ (engagement-filtered) |

This ladder unifies three shipped mechanisms into one knob: the reflex
escalates descent depth per starvation; the circuit breaker is the
ladder's floor (capped raw); graduated steering is its ceiling (level 0,
the null plan).

## Component interfaces (§5, adopted with eight amendments)

Extractor protocol (`family`, `version`, `matches`, `extract`), the
EvidenceGraph (outcome, aggregate, items with id/kind/severity/summary/
failure_class/location/detail_ref, artifacts, parser_warnings), and the
Evidence Contract (decision_unit, outcome-conditional required/preferred/
retrievable tiers, typed loss severities, stable_order, floor/ceiling)
are adopted as proposed, with:

1. **Volatile quarantine** (defect): timing and other volatile values
   live in a designated `volatile` map excluded from graph identity and
   default rendering — `duration_ms` inside `aggregate` would silently
   break byte-identical digests and the cache doctrine with them.
2. **Contracts are TOML, not YAML** (defect): stdlib `tomllib`, the
   existing policy idiom; YAML would be the core's first hard dependency.
3. `matches() -> str | None` (reason, not bool) — detection stays
   deterministic *and explainable*, as the shipped registry already is.
4. `detail_ref` carries a **minted span token over a real stream** — the
   proposed `failure:<name>` selector resolves nowhere, and synthetic
   streams (`#failures`) don't exist. Spans resolve today.
5. **Completeness attestation**: graph gains
   `coverage: {parsed, total_estimate, complete}` — the contract's
   `complete_identity_census` is only checkable if extraction attests
   completeness; otherwise the renderer must declare a partial census
   (the typed form of the pipe-truncation degradation).
6. `causal_rank` v1 = deterministic occurrence order; causal inference
   is a versioned extractor upgrade, never a silent behavior change.
7. **Severity gates dropping; the ladder governs compaction** —
   orthogonal axes. Catastrophic facts are never dropped but may be
   hierarchically compacted (census-of-census) with every rung
   address-bearing. States the resolver's semantics per severity.
8. **Graphs serialize via canonical_json and are content-addressed** —
   yields extraction caching keyed by blob hash, conformance goldens
   pinned to graph bytes (not rendering bytes), and an independently
   testable seam between layers 1 and 3.

## Resolver, renderer, executable contract, outcome tracker (§5.3–5.6)

Adopted: the executable contract; the five-input resolver emitting a
DeliveryPlan (mode incl. the bypass/flood split — steering's null plan
vs the breaker's capped concession — with census/item_summary/detail
knobs, budget triple, and typed `reasons`); the plan-obeying renderer;
the outcome-tracker event vocabulary. Five amendments:

1. **Validate at the selection seam, not the rendering**:
   `validate_selection(selected_facts, contract, graph)` over typed
   facts; rendered-text substring checks are a secondary smoke layer.
   A `required_fraction == 1.0` assert is satisfiable on attested-
   incomplete extractions only relative to parsed facts + a mandatory
   declared-partiality marker.
2. **Reasons are a closed vocabulary** (ledger-event-shaped) — free text
   cannot train epoch tables.
3. **Plans carry a stable `plan_id`** (hash of non-reason fields);
   outcome events record it — P_rerun(p) is unestimable otherwise.
4. **`census="bounded"` means hierarchically compacted and
   identity-preserving** (the ladder), never identity-dropping.
   **The renderer is a pure function** — no signals, no state, no clock.
5. **"Materially narrower execution" is a first-class positive
   ("narrowing")**: a single-test run using a census-delivered identity
   is the census consumed *without* retrieval — stronger than a landing.
   Ledger vocabulary extension is a schema-v2 bump with tolerant readers
   (unknown events → "other"), scorecard updated in lockstep.

## Reader capability (§6) and signature algebra (§7)

**§6 adopted**: ReaderState from existing ledger events; deterministic
prior-weight shrinkage (a Beta-posterior mean without ceremony);
tier = prior, session behavior = evidence, delivery = posterior decision.
Amendments: (1) ModelPrior tables are epoch-compiled from session
ledgers — reviewed, never live-mutated cross-session; (2) preference
transitions follow the latching discipline — dropping to `inline`
latches, recovery is earned (>0.7 followthrough AND a minimum landing
count); `confidence` below a floor defers to epoch defaults; (3)
`inline` governs evidence delivery, never citation — addresses always
ride (provenance); (4) rerun_susceptibility v1 is plan-unconditional
(acceptable); plan_id-tagged outcomes make the conditional split
computable later — version the derivation.

**§7 adopted as a relation algebra**, not just equality:
- *equivalent* — normalization (shipped: slicers, presentation flags,
  wrappers, `python -m`, `ctx run` unwrapping);
- *narrower* — family-specific containment (`pytest a.py::t` ⊂
  `pytest a.py`, node-id prefix): fires the "narrowing" positive —
  census consumed without retrieval;
- *disjoint* — different decision scope;
- × **content equality** (worktree-hash, rule 5 v3) as the orthogonal
  dimension: the full rerun-classification matrix.

**Defect found by §7 (pytest/v2 item):** v1 signature normalization
strips ALL flags as presentation noise — including scope-affecting ones.
`pytest -k auth` / `-m slow` / `--lf` currently equal bare `pytest`,
so a legitimate scope change can score as starvation. Fix: per-family
signature tables declaring scope flags (kept) vs presentation flags
(dropped) — breadth as data, per the house rule.

## Source-state generations (§8)

Adopted as the unifying abstraction over reflex v2's edit-disarm and
rule 5 v3's content equivalence: **rerun classification = signature
relation × generation equality**. Interventions record their generation;
an equivalent rerun in the same generation is evidence recovery, in a
later one it is verification. Progress (rule 9) is measured only across
generations.

1. **Two tiers**: hook events bump generations *provisionally* (cheap,
   steers teaching/latching pre-execution; blind to `sed -i`/`git
   apply`/`echo >>` mutations and fooled by irrelevant edits); capture
   time *confirms* against the worktree hash already minted into every
   run manifest (zero added cost). Equal hash → starvation confirmed
   even if an event fired; different → verification even if none was
   seen. Only confirmed events train [digest_density].
2. **The untracked-content trap**: `_worktree_hash` hashes `git status
   --porcelain`, which lists `?? file` regardless of content — edits to
   just-created unstaged files (the dominant spec-driven-creation
   pattern) don't change it, confirming false starvations on exactly the
   spec3 workload. The generation hash extends with a digest over
   untracked files' (path, size, mtime) — legal because generations are
   operational identity, never content identity.
3. **Freebie — flakiness detection**: within one generation, same
   signature, a nonzero failing-set delta is mechanical evidence of
   flaky tests (deterministic source ⇒ deterministic failures). A
   `flaky` event joins the schema-v2 vocabulary at zero collection cost.

## The canonical picture

```
SAFETY PLANE (never adaptive, spans BOTH ends)
  entry: authorization · confinement · hard caps · timeout
  exit:  redaction · control-stripping at emission
                               │
                               ▼
Command ──► Execute ──► Raw artifacts (CAS) ──► Semantic extractor
                                                     │
                                                     ▼
                                          Evidence Graph (facts + spans)
                                                     │
              ┌──────────────────┬───────────────────┼──────────────────┐
              │                  │                   │                  │
              ▼                  ▼                   ▼                  ▼
      Evidence Contract   Session Outcomes     Signal Record      Policy Epoch
      required census     reruns/landings      window % · tier    reviewed defaults
              │                  │                   │                  │
              └──────────────────┴─────────┬─────────┴──────────────────┘
                                           ▼
                              Delivery Policy Resolver
                                           │
                                           ▼
              PASS_SUMMARY / FAIL_CENSUS / DENSE / BYPASS
              (BYPASS is two states: steering's null plan, entered from
               above at level 0 · the breaker's concession, entered from
               below at the ladder floor, capped raw — the ledger must
               distinguish them or the compiler learns nonsense)
                                           │
                                           ▼
                              Deterministic Renderer
                                           │
                          ┌────────────────┴────────────────┐
                          ▼                                 ▼
                 bounded transcript                  artifact store
                          │                         (spans, manifests)
                          ▼
                 model's next action
                          │
                          ▼
          typed intervention outcome event
          (records the ACTIVE PLAN — the compiler
           needs plan-conditional P_rerun(p))
                          │
                   ┌──────┴──────┐
                   ▼             ▼
             fast session     offline epoch
             reflex/breaker   compiler + review
```

## Build path

1. **pytest/v2 = the first EDC instance** (no new framework): explicit
   pytest Evidence Contract + ladder; worktree-hash rerun equivalence;
   compression circuit breaker (post-densify concession: capped raw,
   failure-budget-bounded); progress events in the outcome ledger.
   Acceptance: the n≥3 paired-seed spec3 referee + flood evals
   unregressed + the rule-7 invariant test (guard decisions for
   secret/escape classes byte-identical under any adaptive state).
2. **The split** (rule 3): `Profile.extract() → Facts` + one
   planner/renderer layer; lint/go/jest/cargo/build contracts ride the
   shared machinery — breadth becomes data (contract tables), not code.
3. **Epoch consumption**: [digest_density] (and successor plan-priors)
   consumed by the planner, trained on progress events, compiled and
   committed as today.
