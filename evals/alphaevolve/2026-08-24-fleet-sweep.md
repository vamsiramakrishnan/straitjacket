# Fleet sweep receipt — 2026-08-24

## Scope

Every registered experiment family ran one bounded managed AlphaEvolve
campaign on engine `phoenix-retail_1751440313229`: 22 families x up to 48
programs (~950 candidates total), flash/pro mixtures rotating
exploration-heavy to exploitation-heavy per round, plateau-bounded rounds,
all spend explicitly confirmed. This receipt reports wins, plateaus, and
failures with equal weight.

## Fleet result (managed best vs local reviewed seed)

| Outcome | Count | Detail |
|---|---:|---|
| Positive delta | 4 | context-budget +1.59 · guard-policy +0.54 · emission-policy +0.30 · execution-policy +0.20 |
| Exact plateau (no candidate beat the reviewed seed) | 17 | incl. turn, route-replay, retrieval, escalation, plan, handoff, verification, mutation, setup, archive, engagement, surface, profile, context-policy, route-policy, escalation-family repeats |
| At seed (new family baseline) | 1 | collab-policy 109.18 |
| Ranking artifact (engine ranking never reflected the strong seed upload) | 2 | naive-fast-path −0.99 · wave-policy −2.30 |

The headline of this sweep is the same pattern the fleet documented on
2026-08-18: tight policy seams stay tight under managed search. The only seam
with real headroom in this entire program remains generic evidence selection,
where headroom was deliberate (seed 75.42) and managed search delivered
+25.22% with transfer — reported separately in
[`2026-08-24-evidence-selection-holdout.md`](2026-08-24-evidence-selection-holdout.md).

## Cross-harness adversarial review (Phase 4)

Winners were reviewed by a different harness than the coordinator through the
native `ctx collab` plane (tasks `t-004…t-005`, reviewer rotation across
codex/gemini/agy), then every claim was refereed locally:

| Winner | Reviewer verdict | Local referee | Disposition |
|---|---|---|---|
| context-budget (+1.59) | agy: reject — greedy allocation drops mandatory items | **confirmed**: adversarial gate fails at −101,000 (mandatory-evidence drop); search+holdout pass | **rejected** — the sweep's only apparent win over +0.5 is blocked by its own gate |
| guard-policy (+0.54) | agy: reject — constants/non-determinism claims | **not confirmed**: all three gates pass (91.95 / 93.95 / 100.00) | advisory disagreement recorded; stays managed-search-ready |

The review gate demonstrably works: it blocked the one sweep winner whose
improvement was bought by silently omitting required evidence — exactly the
failure class the charter forbids — using a second harness's independent read
before any promotion report moved.

## Replay check (Layer B)

Over 772 large tool outputs from 251 recorded Claude-Code sessions on this
machine (`replay_signal_compare.py`), the translated signal ranking is a
strict upgrade over the seed-era keyword regex: 57 decisive upgrades,
52 outputs where old found nothing, zero outputs where old found evidence and
new does not, zero benign-trap selections. Receipt JSON:
`results/2026-08-24-replay-signal-ranking.json`.

## What this means

1. Managed search earns its cost where seeds have deliberate headroom; on
   mature seams it functions as an expensive confirmation engine. Both facts
   are now measured per family rather than assumed.
2. Positive deltas without adversarial transfer are worthless: the single
   largest sweep win failed review and was discarded.
3. Next campaign targets come from measured blind spots, not guesses:
   coordinate-saturated ranking (searchprof/grep digests), multi-line
   remediation blocks, and the two ranking artifacts worth one diagnostic
   run each.

## Limitations

- 48 programs per family is enough to expose plateaus but not exhaustive;
  deeper budgets are only justified for families showing variance.
- Reviewer verdicts are advisory; local frozen gates remain the authority.
- No live canary ran in this sweep; no billed-production savings claim is
  made anywhere above.
