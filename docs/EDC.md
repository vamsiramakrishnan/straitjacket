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
