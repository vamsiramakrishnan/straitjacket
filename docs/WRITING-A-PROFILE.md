# Writing an evidence profile

A profile is not a clever truncation function. It is the implementation of an evidence
contract for one output family.

The profile’s job is to turn raw bytes into typed facts, attest what it understood, and
render the smallest decision-useful view without destroying the route back to the
source.

```text
raw artifact
    ↓
extractor
    ↓
typed EvidenceGraph + coverage
    ↓
Evidence Contract
    ↓
Delivery Policy Resolver
    ↓
DeliveryPlan
    ↓
pure renderer
    ↓
bounded digest + CoverageReceipt + addresses
```

Sections 1–10 below are the design discipline. If you just want to know *where
the code goes*, start here.

## The code: where a profile lives and how to register it

A profile is a small class plus a committed contract. Four concrete steps:

1. **Write the profile.** Create `src/ctx/digest/<family>prof.py` with a class
   that subclasses `Profile` (from `ctx.digest.base`). Set a class attribute
   `version = "<family>/v1"` and implement two methods:
   - `detect(self, ctx) -> str | None` — return a short match-reason string, or
     `None` to decline this artifact;
   - `render(self, ctx) -> str` — produce the digest body.

   `DigestContext` gives you the stream views, the manifest, focus terms, the
   store handle, and helpers (`coverage_lines`, `next_lines`, `mint_span`, …).
   For a census-grade family, have an extractor build an `EvidenceGraph`
   (`src/ctx/evidence.py`) and render through
   `ctx.digest.evidence_render.render_fail_evidence` so the renderer stays pure.
   **`src/ctx/digest/pytestprof.py` is the reference implementation** — read it
   first.

2. **Register it.** Add your class to the `_PROFILES` tuple in
   `src/ctx/digest/__init__.py`. Order is load-bearing: detection is
   first-match-wins in tuple order, and `text/v1` must remain last as the
   universal fallback. Place your profile *before* any more-generic shape it
   could be confused with (e.g. diagnostics before generic search, because both
   carry a `file:line` shape but diagnostics also carry severity).

3. **Write the contract.** Add `src/ctx/contracts/<family>.toml`. It is
   auto-discovered by family name — no registration code. The shape:

   ```toml
   schema  = "ctx.evidence-contract/v1"
   family  = "pytest"
   profile = "pytest/v2"
   decision_unit = "failing_test"

   [outcomes.fail]
   required    = ["aggregate_counts", "complete_identity_census", "location"]
   preferred   = ["one_line_summary", "root_detail"]
   retrievable = ["full_traceback", "stdout", "stderr"]
   [outcomes.pass]     # ...
   [outcomes.default]  # fallback outcome

   [loss_severities]   # catastrophic | major | minor
   complete_identity_census = "catastrophic"

   [rendering]
   stable_order          = "occurrence"
   evidence_floor_tokens = 256
   hard_ceiling_tokens   = 4000
   ```

   The contract is validated over **typed facts** at the selection seam, never
   by re-parsing rendered text. The special `complete_identity_census` class is
   the full item-identity set — the thing that must survive budget pressure.

4. **Emit two renderings per outcome (optional).** If pass and fail should
   render differently, set `ctx.meta_profile_version` inside `render()` (as the
   pytest profile does for `pytest/v2`); `render_run_digest` stamps the digest
   meta from it.

**Tests to add:** profile acceptance in `tests/test_coverage_profiles.py`,
contract validity in `tests/test_contract_conformance.py`, and a family census
test modelled on `tests/test_pytest_census.py`. The [testing
guide](../CONTRIBUTING.md#running-and-writing-tests) explains the CI split your
change must survive.

Everything below is the *why* — the discipline that makes the four steps above
produce a safe digest rather than a clever truncation.

## Before adding a profile

Add a family-specific profile only when the output has a stable semantic shape that a
generic text, JSON, table, or log profile cannot preserve well enough.

A new profile is justified when at least one is true:

- identity must survive budget pressure, such as every failing test;
- the output has a meaningful hierarchy, such as diagnostic → file → span;
- repeated structure can be summarized without losing exceptions;
- machine-readable output exposes facts that prose parsing cannot recover reliably;
- the generic profile repeatedly causes measured re-execution or evidence starvation.

Do not add a profile because a command is popular. Prefer shape over brand. A tabular
profile should serve `kubectl`, `docker`, cloud CLIs, and database clients when their
outputs share the same evidence contract.

## 1. State the decision the digest must support

Write the user/agent question before the parser:

```text
Which tests failed, how did each fail, and where can I retrieve its full traceback?
```

Then name the loss that would make the digest unsafe or useless:

```text
Dropping a failing-test identity can hide a shared cause and trigger repeated reruns.
```

This becomes the profile’s acceptance criterion.

## 2. Define typed facts

Facts should describe the output family, not the renderer’s prose.

Example:

```text
Failure
  test_id
  failure_class
  summary
  frame_file
  frame_line
  traceback_ref
  stdout_ref
```

Rules:

- retain stable identity separately from detail;
- represent source coordinates explicitly;
- quarantine volatile fields such as timing and temporary paths;
- distinguish “not present,” “not parsed,” and “omitted from rendering”;
- use closed vocabularies for machine-consumed reason codes;
- keep raw evidence references on every claim that may need inspection.

Never make downstream policy re-parse rendered text. The selection seam operates over
typed facts.

## 3. Extract once, attest coverage

The extractor reads the captured artifact and emits:

- typed items;
- source references;
- parse warnings;
- coverage counts;
- a declared degraded mode when the family is only partially recognized.

Coverage is the objective; digest size is the constraint.

Useful attestations include:

```text
parsed failures: 37 / 37 identities
parsed traceback blocks: 35 / 37
unmatched lines: 18 / 4,812
input truncated before capture: false
```

Do not claim completeness from the renderer. Completeness must be computed at the
extractor/selection boundary where the full typed result set is still available.

## 4. Write the Evidence Contract

Classify facts by loss behavior:

### REQUIRED

Facts whose absence changes the meaning or hides an identity.

Examples:

- every failing test ID;
- compiler diagnostic severity, file, line, and message identity;
- table header and total row count;
- the existence of an omitted region and its continuation address.

### ELASTIC

Useful detail that may be reduced under budget pressure.

Examples:

- one traceback frame per failure;
- surrounding source context;
- representative rows per group;
- secondary notes and hints.

### RETRIEVABLE

Detail that can leave the first digest only when its exact source address remains.

Examples:

- full tracebacks;
- complete logs;
- large JSON objects;
- source bodies beneath a symbol census.

Define the degradation ladder explicitly. A valid ladder loses detail before identity:

```text
PASS_SUMMARY → FAIL_CENSUS → DENSE → FLOOD
```

Every transition must say what remains, what leaves, and how the omitted evidence is
retrieved.

## 5. Keep policy out of the renderer

The resolver chooses a `DeliveryPlan` from:

- the evidence graph;
- the contract;
- hard safety bounds;
- session state and measured reader behavior;
- the active policy epoch.

The renderer executes the plan. It does not decide the plan.

This separation provides a strong property:

```text
same graph + same contract + same plan = identical bytes
```

If a renderer reads mutable session state, wall time, environment ordering, or ad hoc
budget globals, the contract is already broken.

## 6. Render census before detail

A robust digest generally follows this order:

1. operation identity and outcome;
2. complete required census;
3. ranked or grouped elastic detail;
4. declared omissions and coverage;
5. exact next retrieval commands.

Do not render in parser discovery order when causal or decision relevance provides a
better organization.

A profile should make the common next action obvious without turning every result into
a tutorial. `next:` lines are part of the operational interface.

## 7. Preserve determinism

Normalize or quarantine:

- wall-clock timestamps;
- durations unless semantically required;
- temporary paths;
- process IDs;
- nondeterministic map/set ordering;
- locale-sensitive formatting;
- ANSI and terminal control sequences;
- host-specific interpreter paths;
- random samples.

Use deterministic ranking and explicit tie-breakers. If representative sampling is
required, derive it from content identity rather than randomness.

Golden tests should prove byte identity across repeated renders and equivalent volatile
environments.

## 8. Degrade honestly

Optional parsers and machine-format producers may be absent or malformed. The profile
must choose one of three honest outcomes:

- full-precision family profile;
- labeled lower-precision fallback;
- safe rejection when proceeding would violate the core invariant.

Never emit a high-confidence family digest after a partial parse without labeling the
coverage gap.

Examples:

```text
precision: symbol-level
```

```text
precision: file-level (tree-sitter unavailable; ctags fallback)
```

```text
coverage: 98/100 diagnostics parsed; 2 retained as raw addressed evidence
```

## 9. Test the adversary, not only the fixture

Minimum acceptance matrix:

| Case | Required assertion |
|---|---|
| Small success | Inline-complete or minimal ceremony; no unnecessary retrieval hop |
| Large success | Bounded digest; conclusion survives |
| Single failure | Identity, useful detail, and exact source address |
| Many failures | Complete identity census under pressure |
| Decisive tail | Tail conclusion retained |
| Decisive middle | Omitted line resolvable through a span |
| Malformed output | Labeled fallback; no crash |
| Truncated upstream output | No false completeness claim |
| Mixed stdout/stderr | Correct stream coordinates |
| Volatile rerun | Byte-identical digest |
| Very large region retrieval | Bounded zoom, not recursive flood |

Also test the behavior the profile is meant to change: fewer equivalent reruns, faster
landing on a cited span, or higher task success at the same context budget.

## 10. Add a receipt

Every mechanism should ship with a reproducible receipt containing:

- the frozen fixture or task corpus;
- the baseline and treatment commands;
- constants and budgets chosen before the run;
- success, token, turn, latency, and evidence-preservation results;
- failures and reversals, not only the winning run.

The profile is ready when the evidence shows a mechanism improvement, not when the
sample digest looks elegant.

## Review checklist

- [ ] The decision and unacceptable loss are written down.
- [ ] Extraction emits typed facts with source references.
- [ ] Coverage is attested before rendering.
- [ ] REQUIRED, ELASTIC, and RETRIEVABLE facts are explicit.
- [ ] Policy selection is outside the renderer.
- [ ] Identity survives before detail.
- [ ] All omission is declared and resolvable.
- [ ] Deterministic ordering and volatile quarantine are tested.
- [ ] Small output is not penalized.
- [ ] Degraded modes are labeled.
- [ ] Adversarial fixtures cover tail, middle, flood, malformed, and truncation cases.
- [ ] A behavioral referee measures the intended effect.

---

[EDC architecture](EDC.md) · [Facts and algebra](ALGEBRA.md) · [Concepts](CONCEPTS.md) · [Evaluation receipts](../evals/)
