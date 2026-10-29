# oh-my-pi integration — mechanisms, boundaries, and promotion gates

To use Oh My Pi itself with Straitjacket, run `ctx setup --host omp` and follow
[Agent integrations](AGENT-INTEGRATIONS.md). This page documents the earlier
mechanisms adopted from OMP; it is not the host setup guide.

**Status:** v0.35.0 candidate with bounded canary evidence; opt-in mechanisms remain non-default ([development receipt](../evals/oh-my-pi-integration-2026-08-21.md))

**Upstream studied:** [`can1357/oh-my-pi` at `76a294c`](https://github.com/can1357/oh-my-pi/tree/76a294cb19bfded1e32e2111f1f729129595bf5e)

straitjacket and oh-my-pi solve different problems. oh-my-pi owns an agent
runtime; straitjacket contains context across several existing hosts. We are
therefore adopting useful mechanism contracts, not trying to turn `ctx` into a
second agent runtime. The implementations named below are native straitjacket
code. The upstream links record conceptual provenance; they do not make an
upstream benchmark result a straitjacket result.

This document describes the v0.35.0 candidate. Its owned edit and isolated
worktree paths have deterministic tests and a bounded matched canary; archive
and stream policies remain inactive experiments. Until the release is
committed, treat the API and CLI shape as provisional.

## Status at a glance

| Mechanism | Current development status | Default behaviour |
|---|---|---|
| Addressable edit transactions | `ctx edit plan`, `preview`, and `apply` are implemented with focused tests | Explicit invocation only; host-native edits are unchanged |
| Post-edit diagnostics | Built-in Python, JSON, and TOML checks are attached to owned edit application; an external diagnostic-provider API exists | Built-ins run for `ctx edit apply`; no live LSP provider is configured |
| Isolated typed workers | Worktree patches and schema-validated yields are integrated into `ctx orchestrate` behind configuration | Isolation and globally strict yields are off |
| Lossless visual cold tier | A fail-closed routing policy and frozen policy evaluator exist | No renderer or host transport is connected; visual archival is inactive |
| ctx-owned stream rules | A bounded incremental matcher and activation receipts exist | No production model transport invokes it |

These are scoped mechanism facts. The bounded canary below measures exact
drift cases and a local two-worker route; it does not establish a general
reduction in cost, context, turns, or task-completion loss.

## 1. Addressable edit transactions

oh-my-pi's [hashline recovery](https://github.com/can1357/oh-my-pi/blob/76a294cb19bfded1e32e2111f1f729129595bf5e/packages/hashline/src/recovery.ts)
demonstrates a useful principle: a stale edit may follow content that moved,
but it must not guess after that content changed or became ambiguous.

straitjacket applies that principle in `src/ctx/edit_transactions.py` on top of
its existing content anchors:

1. `plan` resolves every anchored span, snapshots its source file, and seals
   the exact target and replacement behind SHA-256 identities;
2. `preview` re-resolves every target and stores a full unified diff as an
   immutable blob without writing source files;
3. `apply` preflights the whole plan, permits only unique byte-identical
   relocation, stages every output, performs a final cooperative compare-and-
   swap check, and then replaces files; and
4. refusal and application receipts contain locations, sizes, and digests,
   while the plan containing replacement text remains source-sensitive.

Multiple edits in one file must move by the same offset and cannot overlap.
Zero or multiple relocation candidates refuse. This is exact relocation, not
fuzzy patching.

Current limit: the transaction is an explicit `ctx edit` workflow. The host
hooks do not silently translate arbitrary Claude Code, Codex, or Antigravity
edit tools into a transaction. The final compare-and-swap check is cooperative;
an unrelated process can still race after the check and before `os.replace`.

The request is intentionally reviewable JSON. `span` must be an anchored range
minted by `ctx get` or `ctx def`:

```json
{
  "schema": "ctx.edit-request/v1",
  "edits": [
    {
      "path": "src/example.py",
      "span": "12:13@07407f1c",
      "replacement": "def answer():\n    return 42\n"
    }
  ]
}
```

```console
ctx edit plan request.json --out plan.json
ctx edit preview plan.json --receipt preview.json
ctx edit apply plan.json --receipt applied.json
```

`preview` does not write source files. `apply` re-runs preflight rather than
trusting an earlier preview.

## 2. Fresh post-edit diagnostics

oh-my-pi's [LSP write-through path](https://github.com/can1357/oh-my-pi/blob/76a294cb19bfded1e32e2111f1f729129595bf5e/packages/coding-agent/src/lsp/writethrough.ts)
highlights a second ordering problem: diagnostics returned after a write may
still describe the document version from before that write.

`src/ctx/post_edit_diagnostics.py` separates diagnostic contents from their
freshness evidence. `capture_baseline` records the pre-edit document digest and
optional diagnostic version. `verify_post_edit` then labels each provider
snapshot:

- `fresh` only when it names the exact post-edit SHA-256 or reports a numeric
  provider version strictly greater than the captured version;
- `stale` when a provider answered but cannot prove that it inspected the new
  document; or
- `unavailable` when the file type or provider is unavailable or the provider
  fails.

Diagnostic lists and messages are bounded, while the complete normalized set
receives a deterministic fingerprint. Receipts have content-derived IDs, are
written atomically outside the repository, and are verified when reloaded.
The synchronous built-ins check Python without producing bytecode and parse
JSON and TOML. Other languages require an injected provider.

`ctx edit apply` captures baselines immediately before staging and attaches a
diagnostic receipt after commit. A diagnostic failure does not rewrite history:
the edit receipt remains `applied` and discloses diagnostic unavailability.

Current limit: no LSP process lifecycle, formatting request, debounce, or slow
diagnostic delivery is implemented. Host-native edits also lack the ordering
ownership required for a strong freshness claim. Opaque provider versions cannot
prove ordering and therefore require a matching document digest.

## 3. Isolated workers and typed yields

oh-my-pi combines an [isolated task runner](https://github.com/can1357/oh-my-pi/blob/76a294cb19bfded1e32e2111f1f729129595bf5e/packages/coding-agent/src/task/isolation-runner.ts)
with [schema-checked subagent results](https://github.com/can1357/oh-my-pi/blob/76a294cb19bfded1e32e2111f1f729129595bf5e/packages/coding-agent/src/task/structured-subagent.ts).
The corresponding straitjacket seams are `src/ctx/worktree_isolation.py`,
`src/ctx/worker_yield.py`, and their integration in `src/ctx/orchestrator.py`.

When isolation is enabled, independent mutation nodes must declare disjoint
repository-relative targets and start from a clean, exact Git root. Each worker
runs in a detached temporary worktree, its changes become a binary patch,
undeclared changed paths refuse, every patch is preflighted, and the wave is
applied to the real workspace only after all workers succeed. Cleanup runs on
success and failure. Missing targets, overlaps, a dirty root, or a failed
preflight preserve serialized shared-workspace behaviour or fail closed as the
relevant contract requires.

A route node may also declare an output schema. The prompt requests one JSON
value and the returned value is checked by a dependency-free, deliberately
limited JSON-Schema subset. Unsupported schema keywords are rejected instead
of silently ignored. Strict invalid-yield failure remains opt-in.

Current limit: worktree isolation is disabled by default. It depends on Git,
declared targets, a clean root, and a worker that respects its execution
contract. The patch admission layer checks target scope and mergeability; it
does not infer that the worker made the semantically correct change.

Enable the two independent controls in `ctx.toml`:

```toml
[orchestrate]
isolated_worktrees = true
strict_worker_yields = true
```

A coordinator route opts individual nodes in by declaring `targets` and an
optional `output_schema`; `strict_output_schema = true` can fail one node closed
without making every typed node strict. A dirty root never gets cleaned or
reset to make isolation eligible—it stays on the serial shared-workspace path.

## 4. Optional lossless visual cold context

oh-my-pi's [snapcompact package](https://github.com/can1357/oh-my-pi/tree/76a294cb19bfded1e32e2111f1f729129595bf5e/packages/snapcompact)
motivates an experiment in visual recall for older context. straitjacket's
non-negotiable difference is that its content-addressed evidence store remains
the source of truth. A visual representation may be a redundant cold recall
aid only after every omitted byte has an exact retrieval address.

`src/ctx/context_archive_policy.py` currently chooses among:

- `inline_text` when exact evidence has not been secured;
- `address_only`, the safe lossless default after capture; and
- `visual_cold` only at a compaction boundary for a measured, image-capable
  provider, within an image budget, without known secrets, and after quiet-
  needle and structure-recall gates pass.

The mutable policy seam and its frozen search, holdout, and adversarial cases
live under `evals/alphaevolve/archive_policy/`. This is a routing-policy
prototype, not a visual archive implementation. There is no renderer, frame
format, exact frame-to-source coordinate map, provider request adapter, or live
billing receipt yet. Consequently `visual_cold` must not be advertised or
enabled as a product path.

## 5. Stream rules where ctx owns generation

oh-my-pi's [time-travelling stream-rule coordinator](https://github.com/can1357/oh-my-pi/blob/76a294cb19bfded1e32e2111f1f729129595bf5e/packages/coding-agent/src/session/ttsr-coordinator.ts)
shows how a runtime that owns token generation can stop on a targeted pattern,
activate a reminder, and retry.

`src/ctx/stream_rules.py` implements only the transport-neutral state machine:
a bounded rolling text window, ordered regex rules, per-turn deduplication,
cross-turn fire limits, serializable state, and deterministic activation
receipts. A match asks its caller to abort and retry with a supplied injection.
The engine itself neither aborts a request nor starts another one.

This mechanism cannot be retrofitted into a host hook that never exposes the
assistant-token stream. It is eligible only for an SDK or future native runner
where ctx owns streaming, cancellation, retry, and usage accounting.

## Host boundary matrix

| Surface | Owned edit transaction | Fresh diagnostics | Isolated typed workers | Visual cold tier | Mid-stream rules |
|---|---|---|---|---|---|
| Claude Code hooks | Explicit `ctx edit` only | Only inside `ctx edit`; host PostToolUse telemetry is not a freshness proof | Can be selected as a worker when `ctx orchestrate` owns execution | No host adapter | No token-stream control |
| Codex hooks | Explicit `ctx edit` only | Only inside `ctx edit`; host PostToolUse telemetry is not a freshness proof | Can be selected as a worker when `ctx orchestrate` owns execution | No host adapter | No token-stream control |
| Antigravity CLI hooks | Explicit `ctx edit` only | Only inside `ctx edit`; the published hook has no usable output gate | Can be selected only through the orchestrator contract | No host adapter | No token-stream control |
| ctx-owned SDK/native runner | Eligible for direct integration | Eligible for digest/version-aware providers | Orchestrator seam exists | Eligible only after renderer and provider gates | Eligible; transport wiring not implemented |

An explicit `ctx` command has the same semantics regardless of which host
invoked it. What differs is whether the surrounding host lets ctx rewrite a
native tool call, inspect its result, or interrupt assistant generation. See
`docs/HOST-CAPABILITIES.md` for the current hook contract.

## Bounded canary result

The frozen four-case canary replays one identical replacement through a naive
line-coordinate writer and the production edit transaction. On two benign
cases, completion was 50% naive versus 100% ctx; on two changed/ambiguous
adversarial cases, safe preservation was 0% versus 100%. A production-path
local route with two 350 ms deterministic mutation workers was 1.68x faster in
isolated worktrees than serial execution, with both outputs admitted.

Claude Code and Codex each supplied one live valid replacement proposal; the
same proposal fed both arms. Crucially, this did **not** demonstrate a cost
reduction: the tiny proposals loaded roughly 21k–23k structured actual tokens
per host. See the dated [receipt and exact run handles](../evals/oh-my-pi-integration-2026-08-21.md).
The next candidate seam is therefore task-scoped harness/rules surface
selection, evaluated against actual usage and the same correctness gates.

## Frozen evaluation and promotion contract

AlphaEvolve may search a narrow policy seam; it may not redefine the safety
oracle, change the evaluator, read holdouts, or write generated candidates into
`src/ctx`. A winning candidate remains quarantined until a maintainer translates
the mechanism into reviewed source and the following evaluation contract passes.

Every campaign must freeze before search:

- the task corpus, naive-host baseline, current-ctx baseline, train/holdout/
  adversarial split, random seeds, tool and model versions, and provider prices;
- hard gates for task completion, exact evidence preservation, address
  resolvability, path confinement, deterministic receipts, and fail-closed
  ambiguity;
- measured outcomes for completion, first-attempt success, visible input and
  output tokens, billed cost, turns, tool calls, wall time, retries, and
  incorrect-target or evidence-loss rate; and
- the promotion rule, including confidence intervals or repeated-run policy.
  A percentage without denominators and matched baselines is not admissible.

Mechanism-specific adversarial gates are also required:

| Mechanism | Required frozen cases before default-on promotion |
|---|---|
| Edit transactions | unchanged, uniquely moved, changed, missing, ambiguous, overlapping and differently shifted spans; multi-file preflight and rollback failure; mode preservation; binary/non-UTF-8 refusal; candidate cap; concurrent-writer race |
| Diagnostics | clean and broken supported files; digest match/mismatch; advanced, unchanged, and regressed document versions; unavailable/timeout provider; unsupported and oversized files; more diagnostics than the render bound; diagnostic failure after a committed edit |
| Isolated typed workers | disjoint and overlapping targets; missing declarations; dirty and nested Git roots; out-of-scope changes; binary patch and rename; patch conflict; worker failure; cleanup; malformed and unsupported schemas; cross-host live runs; serial-baseline completion and cost |
| Visual cold context | quiet-needle retrieval, code structure, exact quotations, multilingual and low-contrast text, frame/source coordinate recovery, secrets, corrupted frames, image limits, provider cache behaviour, billed image tokens, and downstream task completion against inline and address-only baselines |
| Stream rules | matches split across deltas, false positives, Unicode, overlapping rules, retry exhaustion, persisted fire limits, pathological regex latency, cancellation failure, duplicate billing, and task completion with and without intervention |

Policy-unit tests are necessary but insufficient. Promotion also requires a
live or faithfully recorded end-to-end path for each claimed host, an immutable
receipt naming all denominators, and an explicit negative-results section. If a
mechanism saves tokens but adds turns, lowers completion, loses exact evidence,
or merely shifts cost into images or retries, that trade-off must remain visible.

## What this integration deliberately excludes

- No claim that oh-my-pi's performance measurements transfer to straitjacket.
- No replacement of exact source bytes with screenshots or summaries.
- No fuzzy selection of an ambiguous edit target.
- No assertion that a PostToolUse event proves diagnostics are current.
- No promise of stream interruption from Claude Code, Codex, or Antigravity
  hooks that do not expose that lifecycle.
- No automatic promotion of AlphaEvolve-generated source.

The intended result is narrower and more durable: learn from mechanisms that
work in an owned runtime, translate only the parts compatible with ctx's trust
boundary, and require task-level evidence before calling any of them an
improvement.
