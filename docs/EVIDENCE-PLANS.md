<sub><a href="README.md">« straitjacket / docs</a></sub>

# Compiled evidence plans (`ctx plan` / `ctx investigate`)

**Date:** 2026-07-19 · design for the post-ALGEBRA wave (doc 5 of 5).
**Status:** shipped v0.25.0 — P0–P3 + the P5 surface (`plan_ir` /
`plan_ops` / `plan_exec` / `astgrep` / `semgrep_engine`, `investigate/v1`
digest + contract, CLI `plan`/`investigate`, MCP observe-tier op; 43
acceptance tests). Outstanding, declared: the P4 telemetry-compiled
`[plan_engines]` cost-table epoch (debt e319eef641 — selection today is
availability-based with disclosure) and the four-arm referee, which needs
live model sessions. Builds directly on EDC (delivery) and ALGEBRA
(derivation); nothing here invents a new policy layer.

## The thesis

Do not make the model explore a repository interactively when it can compile
its intent into a bounded evidence program, execute that program once near
the data, and receive one typed digest.

Today's loop — even under the harness — is:

```
LLM → run → digest → LLM → search → digest → LLM → refs → digest → LLM …
```

Every arrow is a **probabilistic boundary crossing**: a model round carrying
the whole prefix. Bounded digests shrink each `O_i` to `B`, which turns the
naive `O(R·P₀ + R²·Ō)` input cost into `O(R·P₀ + R²·B)` — dramatically
better, but still quadratic in rounds. The remaining pathology is round
count itself, and round count is an artifact of using the model as the
scheduler for deterministic fan-out.

The move: the model pays **one round to compile a plan** — a typed, total,
bounded DAG of evidence operations — the harness validates, prices, and
executes it locally (parallel, cached, provenance-bearing), joins the
results into one evidence graph, and returns **one decision-organized
digest**. Model-visible cost becomes `O(H·(P + D))` where `H` is the number
of *genuine hypothesis changes*, not the number of shell commands:

| regime | model-visible input cost | rounds |
|---|---|---|
| naive interactive | `O(R·P₀ + R²·Ō)` | O(M) ops |
| harnessed interactive (shipped) | `O(R·P₀ + R²·B)` | O(M) ops |
| compiled evidence plan (this doc) | `O(H·(P + D))` | O(H) epochs |

The optimization target is not prompt bytes alone — it is **boundary
crossings** and **prompt entropy**. A plan run leaves a stable transcript
shape (task · plan · plan digest · response); every volatile intermediate
lives in artifacts. Two 2,000-token prompts are not equivalent if one is
byte-stable and the other mutates every turn — this is the prefix-stability
contract, extended from injected bytes to the *shape of a whole
investigation*.

The qualification is "per hypothesis epoch." Some evidence changes the
hypothesis; no static plan survives that. So the control policy is
**epochal**: batch every deterministic fan-out within an epoch; return to
the model only when evidence can materially alter the next plan
(reconnaissance → causal discrimination → repair/verify). Unlimited
replanning degenerates back to the interactive loop and is forbidden by
budget, not by advice.

## Separation of planes (what the LLM stops doing)

The LLM keeps: objective interpretation, hypothesis generation, uncertainty
resolution, repair design, trade-offs. The harness takes: query planning,
physical operator selection, parallel scheduling, joins, dedup, budgeting,
caching, provenance, rendering. This is a database architecture — logical
plan → validation → cost-based physical plan → parallel operators →
evidence graph → ranked bounded rendering — and the analogy is structural,
not rhetorical: an LLM issuing one grep at a time is a human hand-rolling a
join over the network.

Everything below already has a house precedent:

| this doc | existing asset |
|---|---|
| total plan language | `ctx q` (`query.py`): ≤8-stage loop-free pipe, typed kind-chain, MCP-safe by totality |
| per-node artifacts | `ctx seq` per-step `run:` handles; `ctx q` result blobs |
| typed facts + joins | `facts.py` (`decl/imp/fail/changed`, Angle-lite joins) |
| evidence object + contracts + resolver | `evidence.py` · `contracts.py` · `resolver.py` (EDC, shipped) |
| engine fallback + disclosure | `skeleton.py` / `repomap.py` / `codeverbs.py` `_select_engine` idiom |
| priced decisions | PRICED-CONTEXT: `ctx plan price` shows the bill before execution |
| epoch-compiled tables | `policy.py` (`ctx-policy.toml`), reflex outcome ledger |

The two genuinely new objects are the **plan IR + executor** and the
**structural/semantic engine tier** (ast-grep, Semgrep). Neither exists
anywhere in the tree today.

## Naming (binding)

- **Evidence plan** (`ctx.plan/v1`): the model-authored DAG. Never called
  just "plan" in code — the resolver's `DeliveryPlan` already owns that
  word. Modules: `src/ctx/plan_ir.py`, `plan_exec.py`, `plan_ops.py`,
  `plan_cost.py`.
- **Investigation**: one compile→execute→digest cycle (`ctx investigate`),
  manifest kind `investigation`, digest profile `investigate/v1`, contract
  `contracts/investigate.toml`.
- **Epoch** here = *hypothesis epoch* (one plan run). Distinct from the
  three existing epoch meanings (checkpoint, policy, rescue); shares their
  idiom: frozen deterministic set, content-hash identity.

## The plan IR (`ctx.plan/v1`)

Model-authored JSON (stdlib-parsed; canonical-JSON identity; stored as
`blob:` and cited in the digest header, exactly like `ctx eval` scripts).
No YAML — it would be the core's first hard dependency.

```json
{
  "version": "ctx.plan/v1",
  "objective": {"kind": "diagnose", "question": "which changed auth symbols explain the failures?"},
  "budget": {"wall_seconds": 120, "max_nodes": 24, "max_fanout": 64,
             "max_digest_tokens": 1800},
  "steps": [
    {"id": "changes", "op": "repo.changed", "args": {"base": "HEAD~1"}},
    {"id": "outline", "op": "ast.outline", "foreach": "changes.files", "cap": 40},
    {"id": "calls",   "op": "ast.search",
     "args": {"language": "python", "pattern": "$AUTH.authorize($ARG)"},
     "paths_from": "changes.neighborhood"},
    {"id": "tests",   "op": "test.run", "args": {"command": "pytest -q"}},
    {"id": "culprits","op": "evidence.join",
     "inputs": ["tests.failures", "changes.symbols"], "on": "failing_in_changed"},
    {"id": "taint",   "op": "semantic.taint", "when": "culprits.count > 0",
     "args": {"rules_from": "repo:.ctx/rules/auth.yaml"}, "paths_from": "culprits.files"}
  ],
  "emit": {"rank_by": ["dynamic_confirmation", "changedness", "causal_proximity"],
           "sections": ["conclusion_candidates", "counterevidence", "coverage"]}
}
```

### Static validation (fail before execution, never during)

The validator is the totality proof. All checks are static and produce
typed, closed-vocabulary rejection reasons (ledger-shaped — free text
cannot train epoch tables):

- **DAG only**: cycle rejection; `max_nodes ≤ 24` (config-capped); edges
  only to declared upstream ids.
- **Typed I/O**: every op declares input/output kinds (the `ctx q`
  kind-chain check, generalized from a linear pipe to a DAG). Kind
  mismatch is a validation error with the expected chain named.
- **Bounded fan-out**: `foreach` only over an upstream *set-kind* output,
  with a mandatory `cap`; runtime truncation beyond the cap is executed as
  declared omission (count + continuation address), never silently.
- **Guards, not expressions**: `when` admits only a micro-grammar
  (`<node>.count <op> <int>`, `<node>.outcome == pass|fail`). No
  arbitrary predicates — computed control flow remains `ctx eval`'s job,
  off the MCP tier.
- **Capability check**: every op declares `class = observe | execute` and
  its engine requirements. A plan is validated against the *capability
  manifest* of this workspace (which engines are present, which tier is
  calling). Execute-class ops in an MCP-tier plan → rejection, not
  degradation. An op whose only engines are absent → per-op `on_missing`:
  `degrade` (run fallback engine, labeled) | `skip` (declared) | `fail`.
- **Failure semantics**: per-node `on_error: fail | skip_dependents |
  degrade` (default `skip_dependents`); a skipped subgraph is declared in
  coverage, and `emit` always renders.
- **Budget sanity**: node-count × per-op static cost class ≤ plan budget;
  `ctx plan price` renders the estimate (the PRICED-CONTEXT idiom:
  the price appears before the spend, with the cheaper alternative named —
  here, "this plan ≈ N s local · 1 round · ≤M digest tokens vs ~K
  interactive rounds").

Why not arbitrary Python: `ctx eval` already exists for computed control
flow and stays. The plan language deliberately gives up Turing-completeness
to buy static cost estimation, capability validation, safe parallelization,
operator-level caching, plan inspection, and MCP-tier eligibility — the
same trade `ctx q` made, extended from 8 linear stages to a bounded DAG.
Totality is the qualifier for the voluntary tier; a DAG scheduler must
carry its own static bound to inherit it, and `max_nodes × max_fanout ×
per-op caps` is that bound.

## Operator inventory (logical ops → physical engines)

Ops are registered like q-stages — `register_op(name, fn, *, input_kinds,
output_kind, klass, engines, cost_class, doc)` — a strict generalization of
`query.register_stage` with capability class and an engine chain. Every op
result is a derived CAS artifact (canonical JSON, content-addressed) so
node results are individually addressable and reusable.

| logical op | class | physical chain (first available wins; disclosed per node) | exists today as |
|---|---|---|---|
| `repo.changed` | observe | git porcelain + `generation_hash` | `execution.py`, `rundiff.py` |
| `repo.inventory` | observe | `repomap` (grimp → builtin) | `repomap.py` |
| `ast.outline` | observe | **ast-grep** → tree-sitter → ctags → stdlib ast | `skeleton.py` (+ new first rung) |
| `ast.search` | observe | **ast-grep** → tree-sitter query → anchored rg (precision-labeled) | new |
| `ast.rewrite.preview` / `.apply` | execute | **ast-grep** only (no lossy fallback) | new |
| `code.search` | observe | rg → python engine | `_retrieval/` |
| `code.refs` / `callers` / `callees` / `impact` | observe | SCIP (when ingested) → facts → jedi → ast | `codeverbs.py`, `callgraph.py`, `facts.py` |
| `code.related_tests` | observe | facts join (`ref ⋈ decl` into `tests/`) → path heuristic | new (thin) |
| `semantic.search` / `semantic.taint` / `semantic.policy_scan` | observe | **Semgrep** only; absent → declared skip | new |
| `test.run` | execute | `run_capture` + family extractor (pytest/v2 …) | `execution.py`, `pytestprof.py` |
| `evidence.join` | observe | facts Angle-lite joins (`failing_in_changed`, `shared_cause_groups`, `untouched_failures`, `symbol_neighbors`) | `facts.py` |
| `evidence.group` / `rank` / `top` / `where` / `sample` | observe | q combinators | `query.py` |
| `q.pipe` | observe | any existing `ctx q` pipeline as one node | `query.py` |

Notes binding the table:

- **The model specifies epistemic intent, not engines.** "Find likely
  callers" is `code.refs`; the planner picks SCIP vs facts vs jedi vs
  ast-grep vs rg. The shipped `_select_engine` idiom is exactly this,
  availability-based; phase P4 makes it cost-based. Selection is
  deterministic given (availability, freshness, policy epoch) and the
  chosen engine is disclosed in the node's coverage row — fallbacks are
  never anonymous (CONTRIBUTING rule).
- **Execute-class ops are host-visible.** SPEC §10.4 keeps command
  execution on the host's native command tool so the permission flow stays
  visible. `ctx investigate` from the CLI runs execute-class nodes through
  the same guard/confinement as `ctx run`. The MCP tier accepts
  **observe-class plans only** — the read-only investigation is the
  TokenSave-shaped voluntary tier's biggest capability gain, and it gains
  it without gaining code execution.

## The ast-grep tier

ast-grep is a compiled, parallel, multi-language structural grep + codemod
engine over tree-sitter ASTs with `--json=stream` output. It slots in as an
**opportunistic binary** (the ripgrep pattern — on PATH it accelerates and
enriches; absent, deterministic fallbacks carry the same output contract):

1. **High-precision structural retrieval** (`ast.search`): code-shaped
   metavariable patterns (`$CLIENT.authorize($ARG)`) skip comments,
   strings, and formatting noise — precision raised *before* bytes reach
   the model. Matches parsed from JSON, sorted `(path, line, col)`,
   normalized to repo-relative paths, each match snapshot-backed and
   span-minted like every search result today.
2. **Outline acceleration**: a new first rung in `skeleton.py`'s chain.
   Same `ctx.skeleton/v1` schema, same derived-blob caching keyed by
   source blob hash; the contract does not change when the backend does.
3. **Mechanical rewrites** (`ast.rewrite.*`): the deterministic
   transformation tier below constrained patching and free-form editing.
   `preview` emits a diff digest + the full patch as a minted `blob:`;
   `apply` is transactional (`git apply` of the previewed patch,
   all-or-nothing), requires the preview handle, and is
   **generation-guarded**: refuses if `generation_hash` changed since
   preview. An 80-call-site migration becomes one rule, one preview, one
   sampled-edge-cases digest, one apply — not 80 model edits.
4. **A query IR the model can actually author**: patterns look like source
   with metavariables. A richer engine whose query language causes retries
   is asymptotically worse at the whole-system level; authoring
   reliability is a first-class selection criterion.

Determinism: ast-grep version is probed once, disclosed in headers, and
participates in node cache keys. Pattern + language + paths + file blob
hashes + engine version → content key; identical inputs, byte-identical
node artifacts.

## The Semgrep tier

Semgrep answers **constrained semantic questions** ast-grep cannot:
constant propagation, qualified-name/import resolution, and taint
(sources → propagators → sanitizers → sinks). Division of labor is fixed:

| need | engine |
|---|---|
| outlines, census, code-shaped matching, codemods | ast-grep |
| import-aware / constant-aware matching, source-to-sink, policy packs | Semgrep |
| compiler-grade symbol graph | SCIP ingestion (ALGEBRA M-G, unchanged) |
| dynamic evidence | tests, runs, traces — the artifacts we already hold |

Semgrep does not replace the fact store; it is **another fact producer**.
Findings normalize to typed facts (`finding(rule, file, line, span)`;
taint traces as ordered frame lists, every frame span-minted) and join the
same graph as ast-grep matches, SCIP xrefs, git generations, and test
failures.

Packaging: `[sem]` pip extra (it is a Python package; heavier than the
core allows as a hard dep). **Hermetic invocation is binding**: local rule
files only (committed in-repo or plan-inline as a `blob:`),
`--metrics=off`, no version check, no registry fetch at runtime — a
network-fetching analyzer inside a deterministic evidence plane is a
non-starter. Semgrep's version participates in cache keys and disclosure;
findings are sorted and path-normalized before storage. Absence is a
declared skip, never an error (`on_missing`).

## The cost-based physical planner (`plan_cost.py`)

Phase P0 ships availability-based resolution (the existing idiom). Phase
P4 upgrades it to cost-based:

- Each physical engine registers a **descriptor**: cost class (index-read /
  cheap-scan / heavy-scan / process-spawn), precision class (exact /
  resolved / structural / textual), freshness probe (is the index newer
  than the worktree generation?).
- Resolution: cheapest engine whose precision satisfies the op's declared
  requirement and whose freshness probe passes. Deterministic given
  (availability, freshness, epoch table) — same inputs, same choice, no
  runtime learning.
- Initial cost tables are committed hand-estimates; they become
  **epoch-compiled** from per-node telemetry (duration and hit-quality
  accumulate in the ledger; `ctx policy compile` distills a reviewed
  `[plan_engines]` table) — the policy.py idiom, no new machinery.
- The model never chooses rg vs ast-grep vs Semgrep. It cannot: the plan
  IR has no engine field.

## The executor (`plan_exec.py`)

- **Scheduling**: topological order; independent nodes run concurrently
  under a worker cap; result *ordering and identity are completion-time
  independent* (deterministic tie-break by node id) so parallelism never
  touches bytes.
- **Per-node artifacts**: every node result is a content-addressed derived
  blob; execute-class nodes are full `run:` captures (birth gate,
  unchanged). The investigation manifest (`ctx.investigation/v1`) lists
  plan blob, node → artifact map, engines used, coverage, and the
  generation hash it ran against.
- **Node caching**: key = (op, canonical args, input artifact ids,
  relevant file blob hashes or generation hash, engine id+version).
  A replan re-executes only the frontier that changed — the longest
  unchanged prefix of the previous epoch is free. This is what makes the
  one-replan allowance cheap.
- **Budgets**: wall clock enforced per node and per plan (`killpg`
  discipline inherited from `execution.py`); byte caps per node; the
  digest budget is enforced by the EDC resolver like every other family.
- **Safety plane (non-adaptive, outside the plan space)**: workspace
  confinement, secret-path denies, redaction at rendering — none of it
  selectable by any plan, engine choice, or epoch. Execute-class nodes
  pass the same PreToolUse-equivalent guard as interactive commands.
  A plan cannot address outside the workspace; `..`/symlink escapes are
  rejected by the same `ws.confine` everything else uses.

## Rendering: one materialized answer, not a concatenation

A naive batch runner would concatenate 20 summaries — transcript pollution
relocated, not removed. Instead all node outputs normalize into **one
evidence graph** and render by decision relevance, never by command order
(command order is implementation provenance; the model needs causal
organization).

Graph: `ctx.evidence-graph/v2` — the shipped v1 item-set plus an optional
`relations` tuple (`(from_id, relation, to_id, confidence)`), closed
relation vocabulary (`span_contains`, `symbol_identity`, `frame_of`,
`changed_in`, `taints`). This honors "fact lists before fact graphs":
relations arrive now because a consumer finally exists — the join/rank
renderer. Additive, volatile-quarantined, canonical-JSON, content-addressed
exactly as v1; v1 graphs upcast losslessly (empty relations).

Digest `investigate/v1`, governed by `contracts/investigate.toml` through
the shipped resolver (no new policy machinery):

- REQUIRED: objective echo; conclusion-candidate census (each candidate
  carries ≥1 resolvable handle and the planes supporting it — a candidate
  supported by static+dynamic+temporal outranks single-plane); the
  **counterevidence section** (present even when empty: "counterevidence:
  none found in N probes" — the anti-anchoring guard); the **coverage
  attestation** (nodes run/skipped/failed/degraded, engines used, files
  inventoried, failures mapped, raw→visible line ratio); declared
  omissions with continuation addresses.
- RETRIEVABLE: every node artifact —
  `ctx get plan:<id>#<node>` / `#counterevidence` / `#candidate`.
- Ranking keys are a closed vocabulary computed from extracted facts, never
  invented prose: `dynamic_confirmation` (fail-fact join), `changedness`
  (changed-generation facts), `causal_proximity` (join cardinality / frame
  distance), `semantic_confidence` (engine precision class). Shared-cause
  labels are extracted keys (EDC §12.2), verbatim.

Quality is the non-obvious constraint: a tiny digest with 50%
decisive-evidence recall is worse than a larger one with 99%. The
objective is `recall_decisive × precision_presented × coverage_confidence`,
with sufficiency a contract constraint, never a priced term — the EDC
objective function, unchanged, applied to a new family.

## `ctx investigate`: epochal control

```
ctx plan validate <plan.json>      # typed verdict, no execution
ctx plan price    <plan.json>      # est wall · nodes · tokens · vs-interactive delta
ctx plan run      <plan.json>      # execute → investigation digest
ctx investigate --objective "..." [--replans 1] [--budget-tokens 1800]
```

`ctx investigate` is the packaged loop: the model (or skill-guided host)
authors a plan for the current epoch; the harness validates, prices,
executes, digests. The default control policy — one reconnaissance plan,
at most **one** causal replan, then patch/verify — is a budget
(`--replans`, config default 1), not advice. Replans reuse the node cache;
the epoch chain is recorded in the investigation manifest so `ctx replay`
can score it.

Reflex integration: every investigation emission is an intervention with
the declared hypothesis "next model action addresses a ranked candidate,
retrieves one addressed section, or spends the replan." Outcomes (landing,
narrowing, replan, abandonment) join the existing ledger vocabulary
(schema-v2 bump, tolerant readers) and train the `[plan]` epoch tables the
same way digest density is trained today.

## Plan value: follow-up statistics and shadow ranking (shipped, reshaped)

Reshaped 2026-07-19 after a design review whose verdict we accepted: the
first version dressed follow-up **association** in causal language
(attribution, confidence, validation) and jumped from observational
telemetry to a weighted decision theory. The governing law now:

> Measure associations first. Demonstrate counterfactual value in shadow.
> Promote only proven choices into conservative tie-breaks.

```
plan node / command emits evidence
        ↓ identities recorded (handles, spans, symbols, test ids, files)
subsequent commands / retrievals / edits / tests observed
        ↓ exact-match joins (evidence_outcomes.followup_join)
evidence_followup/v1 events — match classes, four states, no floats
        ↓ offline aggregation (ctx policy compile --plan-value)
[plan_value] COUNTS table, committed like code
        ↓ read-only at runtime
per-operator report (ctx replay --outcomes) · shadow ranking
(ctx investigate --advise / ctx plan price --value) · shadow ledger
        ↓ paired referee (pending)
ONLY THEN: a conservative tie-break between semantically equivalent actions
```

**The event** (`evidence_followup/v1`): match classes instead of a
confidence float — `exact_handle · exact_span_overlap · exact_test_id ·
exact_symbol · exact_file` — because ``exact_handle`` is easier to review
than ``confidence = 0.98`` and the float suggests calibration that does not
exist. Four states: `used_exactly` (an exact emitted identity was acted
on), `validation_associated` (an associated edit was followed by a passing
verifier — association, NOT causation; verifiers naturally follow edits),
`equivalent_requery` (same normalized signature, no intervening generation
change — reusing the reflex signature + scope-flag tables), `censored`
(the window never closed; session end is never negative evidence). Finer
distinctions return only when a measurement proves they carry signal.

**The table**: counts, never rates — ``observations / used_exactly /
validation_associated / equivalent_requery / censored`` plus cost
lower-medians where events carry them — so 2/2 can never masquerade as
100% in a committed artifact. Wilson lower bounds are derived at read
time.

**The ranking** (shadow only, lexicographic, no weighted scalar):
hard constraints (caller-side, always) → precision class (exact/semantic
before structural before textual) → freshness → Wilson lower bound of
exact-use → Wilson bound of validation-association → requery ascending →
median tokens → median ms → name. The explanation IS the key.

**What is deliberately absent** (deferred until the paired referee — which
compares declared vs shadow orderings on paired tasks at equal success —
answers for them): weighted utility scalars, fractional evidence-coverage
arithmetic, automatic stopping (the report emits a low-yield *sentence*,
suppresses nothing), batch scheduling, language-partitioned cells (the
language field is captured on events for a future interaction-effect
test; priors stay global), and any autonomous choice of the next logical
action. The evidence-dimension vocabulary survives as *descriptive* plan
metadata: `requires` floors validate and display (UNMET lines over
REALIZED coverage — an op's declared `provides` counts only when its node
produced rows), and never gate.

Known confound, stated in the artifact: per-operator follow-up rates are
entangled with WHEN operators run (verifiers sit near the end of
successful trajectories; reconnaissance sits at the start). That is why
the counts feed a report and a shadow ledger — not behavior.

Seeded mechanistic acceptance: [`evals/plan_value_selection.py`](../evals/plan_value_selection.py)
(strong-record preference, 2/2-vs-68/84 sample honesty, disagreement
reported-never-enforced). Runtime never writes the committed policy; the
ledger is appended only by explicit `ctx replay --outcomes
--append-ledger` or plan integration.

## Phases

Each phase lands with its acceptance tests in the same change
(CONTRIBUTING rule); the containment regression suite (floods, needle,
determinism, cache stability, confinement) is a non-regression gate at
every step.

**P0 · Plan IR + validator + executor over existing ops** (~3–4 days)
`plan_ir.py`, `plan_exec.py`, `plan_ops.py` wrapping only shipped
machinery (`repo.changed`, `ast.outline`, `code.*`, `test.run`,
`evidence.join`, q combinators, `q.pipe`). CLI `ctx plan
validate|price|run`. No new engines, no new digest profile — output is a
provisional census render.
*Gate*: validator rejects cycles/over-budget/kind-mismatch/execute-on-MCP
with typed reasons; byte-identical investigation artifacts across replays
on an unchanged worktree; every node artifact resolvable via `ctx get`;
minimal `[dev]` install passes with zero optional engines.

**P1 · EvidenceGraph v2 + `investigate/v1` digest + contract** (~2–3 days)
Relations tuple; graph merge across nodes; `contracts/investigate.toml`;
plan-obeying renderer through the shipped resolver; ranked sections,
counterevidence, coverage attestation; `plan:<id>#section` addressing.
*Gate*: Rule-14 property test (same graph+contract+plan → identical
bytes); contract-conformance suite (REQUIRED classes present on seeded
fixtures); v1 graphs upcast losslessly; degradation order property test
(teaching drops before evidence; candidates never drop, they compact).

**P2 · ast-grep tier** (~2–3 days)
`ast.search` engine chain (ast-grep → tree-sitter → anchored-rg, precision
labeled); skeleton first-rung acceleration behind the unchanged
`ctx.skeleton/v1` contract; `ast.rewrite.preview/apply` with transactional,
generation-guarded apply.
*Gate*: match parity harness between engines on a fixture corpus with
precision-class labeling; byte-identical results across runs; apply
refuses on generation drift and applies all-or-nothing; absence of the
binary degrades with a labeled note, never errors; version in cache key
verified.

**P3 · Semgrep tier** (~2 days)
`[sem]` extra; `semantic.search|taint|policy_scan`; hermetic invocation
enforced by construction (no network, local rules only); findings → typed
facts joining the shared graph.
*Gate*: taint fixture (seeded source→sink with and without sanitizer)
produces the finding with every frame span-resolvable; absence → declared
skip; version-keyed caching; no network syscalls in the sandboxed test.

**P4 · Cost-based planner + telemetry** (~2 days)
Engine descriptors, freshness probes, committed cost tables; per-node
telemetry into the ledger; `ctx policy compile` learns `[plan_engines]`.
*Gate*: selection deterministic under fixed availability; disclosed per
node; epoch table changes engine choice only through a committed policy
file, byte-diffable in review.

**P5 · MCP tier + `ctx investigate` + measurement** (~2–3 days)
MCP `op: investigate` accepting observe-class plans only; the packaged
epochal verb with `--replans`; reflex hypothesis/outcome events; the eval.
*Gate*: MCP-tier execute-class rejection test; the four-arm referee below.

## The referee (frozen before P5 lands)

Four arms on two frozen fixtures — a seeded multi-failure "auth refactor"
diagnosis task (hypothesis-stable) and a cache/context-leak task whose
first evidence overturns the obvious hypothesis (hypothesis-sensitive):

- **A** naive interactive · **B** shipped harness interactive ·
  **C** one compiled plan · **D** compiled plan + one adaptive replan.

Metrics, per the measurement doctrine (medians across seeds; counterfactual
metrics labeled estimates with derivation formulas): model boundary
crossings; turns to first correct hypothesis; tool ops per model round;
raw→visible evidence ratio; decisive-evidence recall against frozen answer
keys; re-execution avoided (cache hits on replan); prefix-cache stability;
critical-path wall time; success-adjusted cost.

Predictions on record: B crushes A on context cost; C crushes B on turns
and latency; D beats C only on the hypothesis-sensitive fixture; C beats D
on the known-shaped task; unlimited replanning (D with `--replans ∞`)
regresses toward B. Ship gates: C ≥ B on turns at no correctness loss;
D wins the hypothesis-sensitive fixture; no regression on the flood/needle
suite.

## Non-goals

- No arbitrary code on the MCP tier — computed control flow stays in
  `ctx eval`, CLI-only; the plan grammar will not grow expressions.
- No unbounded replanning; the replan allowance is a budget with a low
  committed default.
- No network access from any plan operator; no Semgrep registry fetches;
  no telemetry emission from engines.
- No daemons before the broker era (M-E); warm indexes arrive there.
- No lossy fallback for rewrites: `ast.rewrite` without ast-grep declines;
  a textual approximation of a codemod is the failure mode, not a feature.
- No renderer-invented prose in rankings or group labels — extracted keys
  only.
