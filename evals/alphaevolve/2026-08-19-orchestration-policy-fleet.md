# Orchestration policy fleet receipt — 2026-08-19

## Question

Can AlphaEvolve improve how Straitjacket uses Antigravity, Claude Code, and
Codex in parallel while reducing context, turns, latency, and cost without
weakening task completion, workspace safety, evidence retention, or mutation
verification?

## Reviewed product integration

Four bounded policy seams were added to production and to the AlphaEvolve
portfolio:

| Lever | Production behavior |
|---|---|
| wave scheduler | parallelize independent read-only frontier nodes within worker/provider limits; run reads before writes |
| mutation isolation | serialize shared-workspace mutations; parallel worktrees remain modeled but inactive |
| handoff budget | choose address-only, compact, standard, or expanded evidence while retaining an exact blob address |
| verification route | independently verify high-risk mutations on another capable host when available; preserve explicit host/model pins |

Generated programs never wrote to `src/ctx`. The production policies are
maintainer-reviewed translations with focused integration tests.

## Local frozen-evaluator result

`python -m evals.alphaevolve.portfolio --wave orchestration --all-local`
completed every search, holdout, and adversarial gate. Percentages below compare
the reviewed integrated policy with each frozen evaluator's complete baseline;
they are modeled policy results, not provider-billed savings.

| Policy | Search result | Holdout result | Important limit |
|---|---|---|---|
| wave | latency -31.50%, turns -20.00%, visible tokens -13.33% | latency -53.75%, turns -37.50%, visible tokens -22.22% | dollars unchanged in this model |
| mutation | dollars -21.88%, latency -31.25%, turns -25.00%, tokens -22.06%; tool calls +8.33% | dollars -6.25%, latency -23.61%, turns -16.67%, tokens -11.76%; tool calls +33.33% | gains include isolated-worktree cases; the live orchestrator currently serializes all shared-workspace writes |
| handoff | dollars -53.33%, latency -44.00%, visible tokens -58.34% | dollars -62.50%, latency -50.00%, visible tokens -68.57% | checkpoint-size cost model, not provider billing |
| verification | dollars -47.95%, latency -33.58%, visible tokens -32.98% | dollars -63.45%, latency -42.00%, visible tokens -41.90% | all mutation-verification gates remain mandatory |

Dataset fingerprints were `9268977eb6349325`, `93365cabfdc1e002`,
`125f8d0393307a0c`, and `db047da8cf3de749`, respectively.

## Massive promotion matrix

`python -m evals.alphaevolve.orchestration_matrix` crossed ready-node counts,
read/write mixes, provider limits, worker caps, worktree isolation, declared and
overlapping write targets, mutation complexity, alternate-host availability,
failure state, dependency state, and output size.

- cases: **269,696**
- policy failures: **0**
- corpus fingerprint: `5b57f3952b911a02`
- evaluator wall time: 3,042.715 ms on this machine

The matrix is a deterministic promotion gate in
`tests/test_orchestration_policies.py`.

## Managed AlphaEvolve setup

The project scan found no existing engine with
`SOLUTION_TYPE_GENERATIVE_CHAT`. Search engines with default assistants were
not reused because they do not meet AlphaEvolve's engine contract. Setup
created and verified:

- project: `vital-octagon-19612` (`440790012685`)
- engine / `GE_APP_ID`: `alpha-evolve-straitjacket`
- assistant: `default_assistant`
- location / collection: `global` / `default_collection`
- enabled APIs: Discovery Engine and Vertex AI
- official client: `Google-Cloud-AI/alphaevolve-on-googlecloud` pinned at
  `b51ab7a6446d0168bf6db52c6dccbec414a21b3f`

Resource identifiers are stored in ignored `.alphaevolve.env`; no credential is
committed. No IAM binding was changed. `ctx setup --repair` also repaired the
local Codex MCP command, and `ctx doctor` then passed all 13 checks.

## Managed campaign results

Every run used explicit spend confirmation and a bounded program cap. The
initial smoke run exposed missing sandbox/brief contracts (`hasattr`, `getattr`,
and exception syntax); validation and all four briefs now reject or explain
those forms explicitly.

| Campaign | Managed experiment | Programs and outcome | Promotion decision |
|---|---|---|---|
| wave smoke | `3304731935868425257` | seed 100; three generated programs invalid | reject all |
| wave rerun | `16046380828090263404` | seed 100; five generated programs invalid or completion-incomplete | reject all |
| mutation | `45664089799859504` | seed 100; four generated programs tied 100 | no strict improvement; reject |
| handoff | `10317762132901266116` | seed 100; four generated programs scored -104,000 | completion gates failed; reject |
| verification | `1738330562277178949` | seed 107.06912; four generated programs tied 107.06912 | no strict improvement; reject |

Independent local inspection of mutation program `12004248020876909686`
confirmed search gains but zero holdout/adversarial gain. Verification program
`6884760995910015416` passed every gate but tied the seed and had no adversarial
gain. Handoff program `18010053574612149254` failed search, holdout, and
adversarial completion cases. No generated candidate was promoted.

## What improved, and what did not

The reviewed orchestration policies improve their frozen complete baselines by
the vectors reported above and are now exercised by production-path tests. The
managed search itself produced **0% incremental improvement over the reviewed
seed policies in this campaign**: two families plateaued and two failed hard
gates. That distinction matters. AlphaEvolve contributed counterexamples,
contract hardening, and evidence that the current seeds are difficult to beat;
it did not justify an invented percentage or an automatic code promotion.

The next credible performance claim requires matched live tasks with complete
provider `actual_usage`, semantic completion labels, and real multi-host
latency. Parallel mutation also requires a production worktree lifecycle before
its modeled reductions can be counted as shipped benefit.

## Verification

- orchestration-focused tests: passed
- full `tests/` suite: passed (`run:bb90c64f1a88`)
- documentation fact/link gates: required before release
- generated candidates: quarantined; zero promoted
