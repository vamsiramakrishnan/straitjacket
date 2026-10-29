# Evaluating semantic context mapping

Use the existing `edit_matrix.py` runner for a paired coding evaluation. It already
starts each arm from identical files, runs an acceptance oracle before and after
the driver, checks target scope and oracle tampering, measures driver duration,
and keeps unavailable cost/token measurements null. Reusing it keeps the patch
success criterion independent of a model's semantic findings.

```bash
python evals/edit_matrix.py --cases cases.json --model exact-model-id \
  --adapter 'native=/absolute/path/native-driver' \
  --adapter 'contained=/absolute/path/contained-driver' \
  --adapter 'semantic=/absolute/path/semantic-driver' \
  --adapter 'adaptive=/absolute/path/adaptive-driver' \
  --repeats 3 --out semantic-results.jsonl
```

These command paths name caller-supplied live drivers; straitjacket does not ship
provider credentials or claim that a fixture driver measures model quality. See
[EDIT-MATRIX.md](EDIT-MATRIX.md) for the case, request, and metrics contracts.

| Arm | Treatment |
|---|---|
| Native | Normal harness context delivery |
| Contained | Deterministic straitjacket capture and retrieval |
| Semantic | Same containment plus explicit depth-one maps over frozen evidence |
| Adaptive | Same map plus host-selected follow-up evidence for unresolved dependencies |

Adaptive selection is an experimental host policy, not an implemented recursive
mode. Its driver must enforce one parent budget across all maps; each prepared
map alone has its own allowance. Include guide, worker, follow-up, failed-attempt,
patch, and verification usage in every arm's total. Do not compare worker-only
cost against a complete native coding run. Pin model/driver revisions and set a
distinct semantic `sample` for every independent trial, so resume does not turn
repeated trials into cached outputs.

Report accepted-patch rates and paired regressions over **distinct cases**, total
provider cost, end-to-end duration, retrieval calls, and retries. Keep repeated
samples identifiable. For semantic QA, use separate human/fixture judgments of
claim correctness, support entailment, counterevidence recall, and dependency
resolution. Valid citation coordinates and complete selected-byte coverage are
not accuracy scores. Search-heavy cases, scattered evidence, misleading local
matches, cross-file dependencies, and small tasks where dispatch overhead dominates
are all necessary strata. Publish losses and partial runs.

`tests/test_semantic_cli.py` integrates the real map mechanism with this independent
runner. Its lying worker returns a supported-coordinate "patch is correct" claim;
the behavioral oracle still fails the unmodified program and passes a correct edit.
These rows are labeled `fixture`, with unknown cost, and cannot establish an RLM
quality, latency, or cost advantage. No live comparative result is claimed by this
implementation.
