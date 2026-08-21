# oh-my-pi mechanism integration — development receipt

**Date:** 2026-08-21

**Upstream pin:** [`can1357/oh-my-pi@76a294c`](https://github.com/can1357/oh-my-pi/tree/76a294cb19bfded1e32e2111f1f729129595bf5e)

**Status:** implementation, deterministic tests, and a bounded matched canary complete; no general product-performance claim

## Question

Can straitjacket translate useful mechanisms from an agent runtime without
weakening its exact-evidence contract or claiming capabilities that cross-host
hooks cannot enforce?

The development arms were:

1. sealed addressable edit transactions with exact stale-span relocation;
2. content/version-bound post-edit diagnostic receipts;
3. opt-in isolated mutation worktrees and typed worker yields;
4. a fail-closed AlphaEvolve seam for a possible visual cold-context tier; and
5. a bounded stream-rule state machine for future ctx-owned transports.

## Protected oracles

- Exact source bytes and command output remain in the content-addressed store.
- Ambiguous or changed edit targets refuse; no candidate may weaken this gate.
- A dirty or ineligible parent worktree is never cleaned to enable parallelism.
- Unsupported worker-schema keywords reject route construction.
- Visual context cannot become the only copy of evidence.
- Hook-only hosts are never credited with token-stream interruption.

## Deterministic verification

| Gate | Result | Evidence |
|---|---:|---|
| Edit/anchor/diagnostic/CLI focused suite | pass | 91 tests, `run:66011f7f7fda` |
| Worktree/orchestrator focused suite | pass | 65 tests, `run:264b79308550` |
| Cross-mechanism focused suite | pass | 164 tests, `run:83aba270c1f8` |
| Repository-wide suite after canary integration | pass | 1,788 test functions collected by the docs referee; exit 0 and 100% progress, `run:b2698060fc8d` |
| Ruff on new archive/stream policy files | pass | `run:dbc1cfb7307b` |
| Changed-file lint, facts, and documentation links | pass | v0.35.0, 1,788 test functions, 305 references, `run:75c36521878a` |
| Bare `pytest` console entry point | pass | eval/canary imports now match `python -m pytest`, `run:d63dacdf1f08` |
| Isolated build and clean-install distribution check | pass | `ctx_harness-0.35.0` wheel + sdist, all host assets present, `run:a3d39dce2faa` |
| Twine metadata check | pass | wheel and sdist, `run:8ca80a742155` |
| Clean-venv installed `ctx edit --help` before version bump | pass | published entry point exposes plan/preview/apply, `run:7af4fe28ddd9` plus installed smoke |

The combined build command in `run:a3d39dce2faa` exits nonzero only because
the base environment lacks Twine; both build and distribution stages passed.
The missing metadata gate was then run in an isolated ephemeral Twine
environment and passed in `run:8ca80a742155`.

The full suite initially caught three repository-contract violations in
concurrent additions: an unreviewed `max(1, ...)` floor, a negative tail slice,
and a bare evidence-width literal. They were repaired before the passing run.
This is recorded because the first full run, not just each feature's local
suite, provided useful admission evidence.

## AlphaEvolve cold-context seed score

The new `archive-policy` experiment has frozen search, holdout, and adversarial
cases. The reviewed seed passed all three local gates. Against the evaluator's
synthetic always-inline baseline, the search cases model:

- **76.56% fewer visible tokens**;
- **71.12% lower modeled dollars**; and
- **no model-turn reduction**.

The candidate was **not Pareto-dominant**, because address retrieval and visual
preparation can add latency or tool calls. These are evaluator projections from
`run:245a02704fab`, not observed provider bills, task completion, or an
AlphaEvolve-discovered improvement. No visual renderer or transport is enabled.

## Matched edit and orchestration canary

[`oh_my_pi_canary.py`](oh_my_pi_canary.py) freezes four edit-drift cases. The
same complete-line replacement is replayed through a naive line-coordinate
writer and the production `ctx.edit_transactions` path. Two benign cases test
task completion; two adversarial cases test preservation/refusal after a
concurrent target change or ambiguous relocation.

| Metric | Naive | ctx | Change | Evidence |
|---|---:|---:|---:|---|
| Benign completion | 50% (1/2) | 100% (2/2) | +50 percentage points | offline production-path replay, `run:8e1aae3e63b0` |
| Adversarial safety | 0% (0/2) | 100% (2/2) | +100 percentage points | changed and ambiguous targets refused, `run:8e1aae3e63b0` |
| Two-worker wall time | 0.762 s serial | 0.454 s isolated | 1.68x observed speedup | local deterministic workers through `run_route`, `run:8e1aae3e63b0` |

The orchestration result uses two 350 ms deterministic workers. Its theoretical
worker-only makespan is 2x; observed speedup is lower because worktree creation,
patch capture, preflight, application, and cleanup are included. It is a local
production-path simulation, not a claim about provider latency.

For the live arm, each host proposed the replacement once and that exact
proposal fed both edit arms. Claude Code and Codex both returned the valid
proposal and reproduced the same matched replay result:

| Host | Status | Wall time | Structured actual usage | Cost evidence |
|---|---:|---:|---:|---:|
| Claude Code / Sonnet 4.6 | live pass | 3.54 s | 21,191 tokens (21,172 cache write; 17 output) | $0.132760 host-reported, `run:c2b8a7bdb44e` |
| Codex / GPT-5.6 Terra | live pass | 5.46 s | 22,549 tokens (11,008 cache read; 13 output) | $0.031767 priced from usage, `run:2ae764187622` |

The first Codex attempt used a non-Git scratch directory and exited before a
model proposal with no usage. The fixture was corrected to the host's normal
Git-workspace contract and only that arm was rerun. It is not counted as a
model failure or hidden as zero usage.

The live result reveals a cost gap rather than a cost win: both hosts loaded
roughly 21k–23k tokens to emit a 13–17-token answer. The transaction improved
replay correctness, but this canary does **not** show reduced host input cost or
turn count. A subsequent optimization wave should minimize the harness/rules
surface for tiny typed tasks and compare actual usage without weakening the
edit gates.

## Negative results and limits

- Edit compare-and-swap is cooperative, and cross-file atomicity uses guarded
  rollback rather than a filesystem transaction.
- Edit transactions currently cover existing UTF-8 text files, not create,
  delete, binary, xattr, ACL, hardlink, or inode-preserving operations.
- Worktrees isolate Git state, not processes, the network, symlink side effects,
  or other operating-system resources.
- Parallel isolated mutation requires a clean exact Git root and one wave of
  disjoint declared targets; later dirty waves serialize.
- Built-in diagnostics cover Python, JSON, and TOML. No LSP lifecycle or slow
  deferred-diagnostic channel is connected.
- The cold-context path is policy-only and inactive.
- The stream-rule engine is transport-only and inactive; Claude Code, Codex,
  and Antigravity hooks do not expose the required token stream.

## Promotion rule

These percentages describe this four-case canary only; none belongs in the
general product headline. Default-on promotion requires larger frozen
task-level gates in
[`docs/OH-MY-PI-INTEGRATION.md`](../docs/OH-MY-PI-INTEGRATION.md), matched naive
and current-ctx baselines, actual usage/billing, repeated runs, and zero loss of
exact evidence or incorrect-target edits.
