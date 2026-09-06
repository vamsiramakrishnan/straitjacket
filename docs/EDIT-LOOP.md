# Editing from observed and verified bytes

Use `ctx edit` to connect an observed source snapshot, a proposed change,
its application receipt, and verification of the resulting files. The workflow
refuses stale or ambiguous source and keeps addresses for its evidence.

The examples use placeholders returned by earlier commands. Model choice and
edit format require separate outcome measurements; an accepted edit receipt
alone does not establish a quality or cost improvement.

Design sources: Stencil's [Harness Playbook](https://stencil.so/blog/harness-playbook),
[Prewalk](https://stencil.so/blog/prewalk), and
[Harness Problem](https://stencil.so/blog/the-harness-problem).

## Observe, preview, and apply

Read the target through an existing repository query:

```bash
ctx get repo:src/example.py --lines 10:12
```

Use the returned `snapshot:` address and the lines you observed. Write the
replacement text, including its intended trailing newline, to a workspace file.
The placeholders below stand for addresses returned by your commands:

```bash
ctx edit replace snapshot:<observed-id> --lines 10:12 \
  --replacement-file replacement.txt --receipt preview.json
ctx edit apply blob:<plan-id> --receipt applied.json
```

`replace` previews by default and returns `planRef`. Applying that address uses
the same sealed plan. Add `--apply` to `replace` for a one-call application when
a separate preview is unnecessary. A live `repo:` address instead requires the
observed content anchor, for example `--lines 10:12@<anchor>`.

Plans derive their target bytes from the recorded snapshot. Apply checks the
current target with full byte hashes, permits a unique unchanged span to move,
and refuses stale, ambiguous, overlapping, or policy-excluded targets. All
files are checked before writing. This is compare-before-write with rollback
on an application error; it is not an atomic multi-file filesystem transaction
against concurrent writers. On refusal, use the receipt's recovery coordinates
to reread and create a new plan.

Full plans, diffs, and applied receipts have immutable store addresses while
retained. Large CLI results return a bounded projection and a `ctx get`
continuation. `--receipt` also writes the complete result to a local JSON file.
The existing JSON request workflow (`plan`, `preview`, `apply`) remains available.

## Verify the resulting behavior

Use `receiptRef` from the applied result. Put verification options **before**
the receipt address; everything after it is the check command:

```bash
ctx edit verify --kind behavior --witness tests/test_example.py \
  --timeout 60 --receipt verified.json blob:<apply-receipt-id> -- \
  python -m pytest tests/test_example.py -q
```

The result carries `verificationRef`. Exit code 3 means a completed verification
failed or became stale. The proof binds the edited files and any declared
witnesses to their hashes before and after the captured command. Missing or
changed inputs invalidate it. Declare relevant test files as witnesses so later
test changes invalidate the proof too; undeclared dependencies are not hashed.

Checks have separate `syntax`, `types`, and `behavior` categories. Syntax-only
checks cannot authorize handoff or expansion. Neither can a no-op edit, failed
check, stale diagnostic, or proof from another attempt. A caller-selected check
can still be incomplete or trivial. Use acceptance tests that discriminate the
requested behavior; a zero exit code is not a complete correctness argument.

For local Python callers, `ctx.edit_transactions.replace_span` accepts
`(workspace, store, ref, span, replacement, apply=False, attempt_key=None)`.
`ctx.edit_verification.verify_edit` accepts a receipt address and 1–8 `Check`
objects, with optional witness paths. Each check has an argv tuple, kind, and a
timeout of at most 600 seconds. CLI and Python writes share edit-outcome
telemetry. These APIs do not add mutation operations to the retrieval MCP tool.

## Continue or expand after one verified edit

For orchestration with prewalk enabled, save a small checklist and investigation
state, then request continuation:

```bash
ctx edit handoff --verification blob:<verification-id> --state state.json
```

The launcher binds the applied edit to its attempt. The orchestrator validates
the emitted state address and proof before handing the same worktree to the
cheaper model. See [Prewalk](PREWALK.md) for the state schema and exact signal.
This is a new launch with a complete bounded checklist and evidence addresses;
it does not restore a provider session or transfer its cache.

For a repetitive edit, supply an explicit structural rule that reproduces the
verified example exactly:

```bash
ctx edit expand --verification blob:<verification-id> \
  --pattern 'old_api($A)' --replacement 'new_api($A)' \
  --lang python --glob 'src/service/*.py' --receipt expansion.json
ctx edit apply blob:<expansion-plan-id> --receipt expanded.json
```

Expansion requires the optional `ast-grep` engine and a single demonstrated
file. It reads frozen copies, restricts scope to at most 64 files and 8 MiB of
input, and returns a normal plan and preview. Apply the returned `planRef` and
verify the expanded edit with tests covering the broader scope. Reproducing
one example proves the rule matches that example, not that every match is
semantically interchangeable.

## Decide from paired outcomes

[Paired edit evaluations](../evals/EDIT-MATRIX.md) compare native, anchored,
and structural adapters on identical tasks with independent acceptance checks.
The same runner supports frontier-only versus prewalk trials. It records case
identity, success, file-scope violations, duration, usage, retrievals, and retries.
Deterministic fixture rows cannot enable a production policy.

```bash
ctx edit advise evals/results.jsonl --model exact-model-id --shape mechanical
ctx edit advise evals/prewalk-results.jsonl --strategy prewalk \
  --model guide-id --executor-model executor-id --shape mechanical
```

Both gates require at least 60 distinct paired live cases, complete cost data,
lower total cost, no loss in observed successes, no candidate file-scope
violations, and a conservative bound on paired regression risk. Repeated runs
of one case do not increase the independent case count. Prewalk cost includes
both models and the handoff/verification work. Validate a selected strategy on
held-out cases before adoption.

Optional [configuration](CONFIGURATION.md)
adds format advice once per launch and gates prewalk by exact model pair and
route-node `edit_shape`. Missing evidence keeps native formatting or the
assigned frontier model. Without a prewalk policy file, `prewalk = true` remains
an explicit experimental opt-in.

No live-model comparison is included in this change. Local receipts and the
evaluation runner are not an isolation or authentication boundary against a
process with the same filesystem access. Stored evidence follows retention and
current redaction policy; missing evidence refuses continuation.
