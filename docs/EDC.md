<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs/edc.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs/edc-light.svg" width="100%" alt="The Evidence Delivery Controller. Architecture work, doc 3 of 4.">
</picture>

<sub><a href="README.md">« straitjacket / docs</a></sub>

# Evidence Delivery Controller (EDC)

> **Design & internals — not product documentation.** This doc explains *why* a
> mechanism exists and how it was reasoned out; it may describe an idea before it
> ships or record one that was rejected. For what the product does **today**,
> prefer [`spec/`](../spec/) and the [changelog](../CHANGELOG.md), and read any
> status label literally. New to the vocabulary? Read
> [How it works](HOW-IT-WORKS.md) and [Concepts](CONCEPTS.md) first.
>
> This one is dense: it's written as a point-by-point adoption of a numbered
> design proposal ("§N adopted with amendments"), so it reads best once you know
> the pipeline it builds — extractor → evidence graph → contract → resolver →
> plan → renderer. [Writing a profile](WRITING-A-PROFILE.md) walks that pipeline
> concretely and is the gentler way in.

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

## Intervention events, the breaker state machine, the three planes (§9–11)

**§9 adopted** — emissions become first-class events
(ctx.intervention/v1) carrying coverage vectors and explicit hypotheses
with bounded windows; outcomes (ctx.intervention-outcome/v1) resolve
them. This upgrades the shipped ledger, which records outcomes only —
P(outcome | coverage, plan) is uncomputable when coverage was never
recorded at emission. Full outcome vocabulary adopted (adds
`workaround`, `validation_after_edit` as a typed positive,
`expired_unresolved`, session terminals). Amendments: interventionId is
deterministically derived (session-seq × signature — replay holds);
handles are minted span tokens; the hypothesis window counts any
tool-bearing command; **expired events are censored observations,
excluded from rate denominators** — silence must not train pessimism.

**§10 adopted** — NORMAL → DENSE → BYPASS as a bounded state machine
with episode semantics (one transition per signature × generation
episode; continued starvation is countable but never re-transitions —
the shipped reflex logged six events for one round-2 episode, which
this forbids) and **hysteresis replacing the permanent latch**:
BYPASS→DENSE after 2 positive outcomes, DENSE→NORMAL after 3
(epoch-tunable defaults). Generation change resets episode state,
preserves reader-capability history. BYPASS renders structured census +
capped raw + declared truncation + full-evidence address, headed
`containment circuit open`.

**§11 adopted** — the three-plane constraint order: safety > evidence
contract > economics. Safety plane is non-adaptive by construction (its
inventory adopted; noted gap: the store has retention-GC but no size
ceiling — filed). **Cross-doc correction**: REFLEX.md's friction
stand-down applies to the evidence plane only (discretionary steering
may concede); safety-class denials never stand down, no matter how
often the command repeats. The evidence plane hosts the fast loop
within hard bounds; the economic plane sets reviewed defaults and may
never override the planes above it.

## Guard classes, concrete plans, resolver, coverage receipts (§11.4–14)

**§11.4 adopted**: every guard declares `class` (safety | usability) and
`adaptive` — the three-plane doctrine becomes a testable property:
safety-class ⇒ adaptive:false, enforced by enumeration plus a property
test that safety decisions are byte-identical under any reflex/circuit/
epoch state. This IS the rule-7 invariant test.

**§12 adopted** (PASS_SUMMARY / FAIL_CENSUS / DENSE / BYPASS / FLOOD
formats) with three corrections:
1. **Addresses are contract-driven, not plan-driven** — §13's
   pass_summary sets include_addresses=False while §12.1's warnings
   variant correctly emits one. Resolution: any nonempty retrievable-
   tier class emits its address in every mode; include_addresses
   governs teaching prose only. Provenance is never a plan knob.
2. **Shared-cause group labels are extracted keys** (file, failure
   class, shared top frame) — never invented topic prose. The spec's
   own determinism guardrail, made mechanical; the example's
   "authentication path" label is the violation class.
3. **Derived artifacts are minted `blob:` handles** — FLOOD's
   machine-readable full census is a content-addressed canonical-JSON
   blob referenced from the digest (put_blob exists; leases/GC
   inherited). No synthetic streams; derived structured artifacts
   become a first-class, generalizable pattern.

**§13 adopted**: circuit state → mode; failure and pressure multipliers;
floor applied after multipliers (pressure never squeezes below the
evidence floor), ceiling last — with `floor ≤ ceiling` asserted at
contract load. The resolver never silently violates coverage: an
unfittable census selects FLOOD with declared partial inline coverage.

**§14 adopted**: the renderer returns (text, CoverageReceipt, plan) —
coverage filled by selection accounting, never by re-parsing output.
Addition: the receipt carries extraction's `attested_complete` so
required_fraction over a partial parse cannot masquerade as full
coverage. Receipt fields feed the emission event (§9) and the existing
raw/emitted telemetry.

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

## Implementation plan (§17), reconciled against shipped code

Adopted with a shipped-status audit — roughly half of phases 0–7 exists
as of v0.21.x:

| phase | status | remaining delta |
|---|---|---|
| 0 · freeze referee | ⚠️ partial | ✅ runner/holdout frozen; ✅ transcripts NOW archived (evals/archive/, all 3 rounds) + versions pinned (py 3.11.15, headroom 0.32.0, claude 2.1.211, haiku-4-5); ❌ n≥3 seed support (debt 34e21fe2dc) |
| 1 · pytest/v2 semantics | ~80% shipped | failure class per row; one-line summaries default; typed coverage receipt |
| 2 · contract infra | ❌ | as specced (TOML; pytest/lint/text first); + guard-class safety property test slots here |
| 3 · extract/render split | ❌ | compatibility adapters; contract version into manifests |
| 4 · one resolver | ❌ | subsumes LADDERS edge 8 (seven independent budget sites); route `run` first, others behind adapters with byte-identical goldens |
| 5 · shadow instrumentation | ~70% shipped LIVE | intervention IDs, intent/execution split, confirmed generations; **new detectors (narrowing, workaround, generation-confirm) ship shadow-first and validate against the archived transcripts** |
| 6 · NORMAL→DENSE | shipped, needs episode semantics | single-transition-per-episode; generation scoping |
| 6b · graduated steering | narrow live arm | one named pytest node on output-substituting hosts; broad classes remain shadow-only |
| 7 · DENSE→BYPASS | ❌ | breaker + hysteresis as specced |
| 8 · generalize families | ❌ | per §16 list; gated on family signature tables (debt 748f470aa1). **Language positioning**: the containment core and v1 digest profiles are already polyglot (go test, jest, cargo/tsc builds, eslint/tsc/cargo/mypy lint — rtk-corpus-measured); phase 8 upgrades them to census-grade extractors + contracts. Priority item: a **universal JUnit-XML extractor** — surefire/gradle, pytest --junitxml, and jest reporters emit the same structured format, so one extractor delivers census-grade evidence for Java/Kotlin and most CI runners with no terminal-prose parsing. Boundary note: EDC extractors parse tool *output*; tree-sitter parses *source* — it belongs to the repo-comprehension verbs (multi-language map/def/refs, the M-B wave) as an optional `[code]` extra with ast/ctags fallback, never to evidence extraction. The existing bounded MCP tool is the TokenSave-shaped voluntary tier and inherits every phase-8 family without a new server. |
| 9 · epochs | schema shipped | consumption, comparison, rollback |

**Ordering lesson, acknowledged**: the plan's shadow-before-enable
discipline (phase 5 before 6–7) is better than what we did — v0.21
shipped detectors and densify together, and the edit-cadence false
positives reached the round-2 ledger before v2 corrected them.
Shadow-first + archived-transcript precision validation is the adopted
discipline for every remaining detector.

**One omission, corrected**: no phase schedules **graduated steering —
the null plan** — which is the highest-expected-value item for the
spec3 verdict (parity on the losing regime by construction, debt
c273b8d3d0). Added as **Phase 6b**: BYPASS-from-above keyed on measured
session scale, shadow-validated like any detector, judged by the same
referee.

**Referee gates**: quick pytest/v2 spot-check after phase 1; the full
n≥3 gate after phase 7 (+6b); per-family spot checks during phase 8.
Flood evals and cache doctrine are non-regression gates at every step.

**§18 test plan adopted**: extraction-conformance suites assert exact
identity lists against real fixtures (the missing test class that let
pytest/v1 ship starved); contract tests assert coverage receipts —
census named 8/8 with required_fraction 1.0 at default budgets —
so contract violations fail CI, not benchmarks.

## Acceptance gates and observability (§19–20)

**§19 adopted as the frozen gate set** for the phase-7(+6b) referee:
- Correctness: holdout 16/16; reviewer scores hold; **census-vs-raw
  cross-validation at benchmark time**; **address integrity** (every
  inline identity's span resolves to that item's evidence); no safety
  guard weakened by any adaptive state.
- Economics: **median across seeds** (the variance-wall lesson encoded):
  sj ≤1.5× naive turns; wall-clock holds; cache advantage ≥ naive;
  same-generation reruns collapse vs the r1 baseline of 8.
- Controller: detection at the 2nd equivalent execution; silent on
  relevant edits and material narrowing; one transition per level per
  episode; bypass bounded; landings vs inline-progress distinguished;
  optional unfollowed hints are censored data, never failures. Replay
  tests run against evals/archive/ transcripts.
- Evidence quality: the degradation order is a parametrized property
  test — teaching drops before evidence, traceback before identity,
  incomplete census only under declared FLOOD, FLOOD always mints the
  structured census blob.
- Regression: the existing containment suite (floods, needle, failure
  asymmetry, head/tail, determinism, cache stability, timeout/signal,
  addressability, retention, confinement) stays green at every phase.

**§20 adopted as scorecard v2**: per-family behavioral outcomes
(interventions, coverage, landings, progressed-without-retrieval,
reruns, transitions), evidence-coverage table, per-signature episode
narratives (the human-auditable false-positive surface), and downstream
cost beyond tokens — with the binding amendment: **counterfactual
metrics ("avoided reexecutions/turns/runtime") are labeled estimates
carrying their conservative derivation formulas**, per the existing
est_cost discipline. Fixes ctx gain's documented token-only framing.

## Migration, non-goals, risks, decision rules (§21–24) — spec closed

**§21 adopted**: legacy profiles ride a compatibility adapter (degenerate
EvidenceGraph, empty items, declared parser warning) — paired
exclusively with the generic fallback contract: a legacy graph is never
validated against a census-requiring contract, and never satisfies one
vacuously.

**§22 adopted wholesale** as permanent scope boundaries — six of its ten
non-goals are this spec's amendments restated as governance (no
per-command starvation, no hint-as-failure, no hidden adaptation, no
learn-and-deploy in one loop, no tier-as-verdict, pytest before
generality).

**§23 adopted**: every mitigation resolves to an adopted mechanism
(generations, shadow mode, bounded escalation, flood ladder,
conservative priors + offline epochs, the single resolver, guard
classes, extraction attestation — "never claim complete coverage the
parser cannot prove").

**§24 adopted**: the fifteen decision rules as the executable summary,
plus **Rule 9b** (the missing credit rule): a run that materially
narrows to census-delivered identities is census consumption — recorded
as a positive narrowing outcome, never starvation. Rule 14 ("same
evidence + contract + plan → identical bytes") is the renderer purity
property test, verbatim.

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
