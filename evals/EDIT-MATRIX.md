# Paired edit-format evaluation

`edit_matrix.py` compares adapters in fresh workspaces with identical cases.
The evaluator runs its acceptance script before and after each adapter. A case
whose script already passes is marked invalid. The script stays outside the
editable workspace; changes to it invalidate the run. Changes outside the
case's declared file targets also fail the run. This does not detect every
wrong-span edit within an allowed file; behavioral coverage still matters.

```bash
python evals/edit_matrix.py --fixture --out /tmp/edit-fixture.jsonl
```

The fixture exercises six mechanical cases through two deterministic adapters.
It uses no model. Its rows say `measurement: fixture`, leave cost/token counts
null, and cannot select a production policy. Tests also check a false success
claim and an out-of-scope edit whose behavioral test passes.

## Live adapters

Supply a JSON list of cases. Each case has `id`, `shape`, `task`, a `files`
object mapping relative paths to source text, `targets` listing allowed files,
and `oracle` containing the Python acceptance script. The oracle must fail on
the initial source and pass on a correct implementation. It executes from the
workspace, with the workspace on its Python import path.

```bash
python evals/edit_matrix.py --cases cases.json --model exact-model-id \
  --adapter 'native=/absolute/path/native-driver' \
  --adapter 'anchored=/absolute/path/anchored-driver' \
  --repeats 3 --out results.jsonl
```

A driver starts in the trial workspace. `CTX_EVAL_REQUEST` names a JSON file
with task, model, format, and declared targets. `CTX_EVAL_METRICS` is the optional
output path for `cost_usd`, `input_tokens`, `cached_input_tokens`,
`retrieval_calls`, and `edit_retries`. The driver must invoke the named model
and collect provider/host usage; absent fields remain null. Exit status and
claimed success cannot override the independent acceptance result.

This interface can wrap installed coding CLIs. It does not obtain credentials
or invoke a provider by itself. The experiment runner labels these trials
`live`; the operator is responsible for using real model adapters. The runner
does not authenticate model identity or self-reported usage. Filesystem and
network isolation must be supplied externally for adversarial evaluations.

Each row records case content identity, model, format, shape, repetition,
independent success, file-scope violation, measured driver duration, usage,
and local capture handles for baseline and acceptance runs. Handles follow the
local store's retention policy; archive the store if long-term raw evidence
is required. Driver order alternates across repeats. Repeated runs of one case
do not count as independent cases when estimating regression risk.

## Selecting a policy

```bash
ctx edit advise results.jsonl --model exact-model-id --shape mechanical
```

The selector requires matching case identities in both arms, at least 60
distinct cases, complete finite cost data, no candidate file-scope violations,
no loss in observed total successes, and lower total cost. Its one-sided 95%
Wilson upper bound on the fraction of cases with any paired regression must
be at most 5%. This is a conservative screening rule, not a universal
non-inferiority proof. Repeat the evaluation on held-out cases before adopting
an experimental result. Fixture, duplicate, incomplete, and mismatched rows
cannot select a new format.

Optional orchestration integration:

```toml
[orchestrate]
edit_policy_file = "evals/results.jsonl"
```

Declare `edit_shape` on route nodes to match the evaluated workload. The exact
model ID and shape select advice once per launch, so the format does not
change within an attempt. The node's shape is persisted for resume. Without
sufficient evidence, native editing remains the default. This is prompt-level
format advice, not tool replacement or additional mutation authority.

## Selecting prewalk

The same runner accepts `frontier` and `prewalk` adapter names. Record the model
identity as `guide-id->executor-id`; the frontier arm runs only the guide, while
the prewalk driver must aggregate **all** guide, executor, handoff, and
verification cost. Then inspect:

```bash
ctx edit advise prewalk-results.jsonl --strategy prewalk \
  --model guide-id --executor-model executor-id --shape mechanical
```

Set `[orchestrate] prewalk_policy_file = "evals/prewalk-results.jsonl"` alongside
`prewalk = true` to require this gate before arming prewalk. Insufficient,
stale-pair, or incomplete evidence keeps the assigned frontier model. Without
that optional file, `prewalk = true` explicitly enables experimental prewalk.
Neither policy grants a handoff without the edit verification gate.
