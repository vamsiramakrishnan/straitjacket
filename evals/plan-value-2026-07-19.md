> **RESHAPED 2026-07-19 (same day, design review):** this receipt records
> the v1 run of a schema since renamed and cut — `evidence_outcome/v1` with
> causal outcome labels and confidence floats became `evidence_followup/v1`
> with match classes and four states; the weighted scorer became a Wilson
> lexicographic SHADOW ranking (report only); the stopping verdict became a
> low-yield advisory sentence. See docs/EVIDENCE-PLANS.md §plan-value for
> the current design and the promotion law. Kept as a historical receipt.

# Plan value — seeded selection eval + end-to-end loop receipt

**Date:** 2026-07-19 · **Harness:** [`evals/plan_value_selection.py`](plan_value_selection.py)
(mechanistic acceptance; deterministic, model-free) · **Loop smoke:** live CLI
run recorded below. This is NOT a cost-savings claim — it is the acceptance
proof that compiled priors change investigation decisions usefully and that
every stage of the loop runs end-to-end.

## Seeded fixtures (all pass; `python evals/plan_value_selection.py`)

**A — cheap join wins.** Changed files + failures known, causality missing.
Candidates: `evidence.join`, `code.refs`, `semantic.taint`, `code.search`.
Selected: **`evidence.join`** (high prior: 84 obs, landing 0.79, validation
0.54; index-class cost; fills causality+changedness). Score 8+ vs <2 for all
others.

**B — expensive semantic scan deferred, then chosen.** With no dynamic
evidence, `semantic.taint` (process-class, medium prior, redundancy 0.32)
ranks below the dynamic/changedness actions. Mid-state honesty: cheap
`code.refs` (0.3 semantic_support at scan cost) legitimately precedes the
scan on value-per-cost. Once the cheaper actions have run and the
source-to-sink question remains, taint ranks first among the remaining
candidates.

**C — hypothesis-sensitive replan.** Floors met → advisory stop fires
(best remaining value 0.00–0.18 < threshold 0.25). A dynamic contradiction
resets causality and adds a counterevidence floor → stop retracts, the
join re-selects, and the batch becomes `[evidence.join, semantic.taint]`
(different missing dimensions ⇒ parallel-admissible).

## End-to-end loop (live CLI, scratch workspace)

```
$ ctx replay --outcomes t.jsonl
Evidence outcomes
──────────────────────────────────────────────────────────────────────────
operator                        obs  land narrow validate redundant censored
profile:pytest/v2                 1  100%   100%       0%         —     100%
attributable: 1/1 events · reasons+confidence per event in --json

$ ctx replay --outcomes --append-ledger t.jsonl   # explicit ledger feed
$ ctx policy compile --plan-value                  # → ctx-policy.toml
[plan_value."profile:pytest/v2"]
observations = 1 · censored = 1 · landing_rate = 1.00 · confidence = "insufficient"

$ ctx plan price --value plan.json
candidate action: evidence.join
missing dimensions: causality, changedness, dynamic_failure
prior confidence: insufficient (0 observations · backoff: builtin)
value score: 2.59
selected over:  repo.changed  1.47

$ ctx investigate --advise plan.json     (after repo.changed + evidence.join ran)
required floors:
  causality          1.00 / 0.80
  changedness        1.00 / 1.00
  dynamic_failure    0.40 / 1.00  UNMET
suggested next action(s):
  code.related_tests   score 0.28 · prior insufficient (0 obs, builtin)
  test.run             score 0.11
```

The advisory correctly identifies the one unmet floor (`dynamic_failure`)
and suggests the two ops that provide it, cheapest first, with the backoff
level disclosed (the 1-observation compiled prior is below
`minimum_observations = 5`, so the builtin conservative prior applies —
low-sample rates never masquerade as high-confidence).

## Attribution limitations (declared)

- Replay approximates generation transitions by edit/write count (recorded
  transcripts carry no git state); live plan integration should pass real
  generation hashes. (debt 936231223f)
- `[plan_value]` carries no cost medians yet — events carry no cost fields
  until plan_exec emits them. (debt 741c6afb40)
- Language-family partition is wired in the backoff chain but unpopulated.
  (debt e7292e571e)
- Task-transition window expiry is not detectable in replay. (debt d9953ed4c4)
- The in-loop scheduler (executor consuming `select_batch`) is behind
  `--advise`; full integration is the next wave. (debt cfc886409c)

Censoring is honest end-to-end: the smoke's own pytest event is censored
(window open at transcript end) and its `landed`/`narrowed` positives still
count while nothing counts against it.
