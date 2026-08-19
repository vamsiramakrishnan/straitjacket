# AlphaEvolve optimization portfolio receipt — 2026-08-17

## Decision

Four completion-gated policy areas were evaluated. The original context,
turn, and route seeds were not beaten in their first 12-program campaigns. A
second naive-fast-path campaign beat the broad baseline on search and unseen
holdout cases, but failed two adversarial cases. No generated policy is
promoted into `src/ctx`.

All percentages below are deterministic simulator estimates, not measured API
billing or production task-success claims.

## Fixed run bounds

- Engine: `gemini-enterprise-17847033_1784703340159`
- Assistant: `default_assistant`
- Location/collection: `global/default_collection`
- Model: `gemini-3.5-flash`
- Per campaign: one seed plus 11 evolved candidates, concurrency 2
- Generated code: restricted AST/imports, child-process timeout, deterministic
  double execution

## General policy campaigns

| Area | Seed / best | Evolved outcomes | Managed experiment | Decision |
|---|---:|---|---|---|
| Context budget | 98.42718 / 98.42718 | 6 ties, 5 invalid | `11039019992973897704` | no improvement |
| Turn policy | 93.38333 / 93.38333 | 8 ties, 3 incomplete | `10704876516988716162` | no improvement |
| Route policy | 106.59000 / 106.59000 | 10 ties, 1 invalid | `7333108742062295445` | no improvement |

Full resources:

- Context: `projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/2673895751047954341/alphaEvolveExperiments/11039019992973897704`
- Turns: `projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/7570338298572817624/alphaEvolveExperiments/10704876516988716162`
- Routing: `projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/14150048546693716091/alphaEvolveExperiments/7333108742062295445`

The plateau indicates that these frozen cases enforce completion but do not yet
separate behaviorally equivalent policies. More varied real traces are needed
before spending on larger searches.

## Naive-fast-path campaign 1

The first brief produced two tied winners at 108.326385 versus the broad seed's
100. Search completion remained 100%, with 11.39% fewer visible tokens, 8.33%
fewer model turns, 10% fewer tool calls, and 11.25% lower estimated dollars.
The winner completed the four holdouts but achieved no holdout efficiency gain,
so it failed promotion.

- Experiment: `projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/6747745152345881495/alphaEvolveExperiments/7274108873648436364`
- Winner checked: `17464412805470814726`
- Outcomes: 2 improved, 1 tie, 5 incomplete, 3 invalid

## Naive-fast-path campaign 2

The brief was corrected to specify task evidence requirements and to direct the
search toward plan capabilities rather than plan-name guesses. Winner
`13931115379999879782` scored 165.368056 versus 100.

| Corpus | Completion | Visible-token reduction | Model-turn reduction | Tool-call reduction | Estimated-dollar reduction |
|---|---:|---:|---:|---:|---:|
| Search, 8 cases | 100% | 90.14% | 66.67% | 75.00% | 88.00% |
| Unseen holdout, 4 cases | 100% | 87.64% | 58.33% | 70.00% | 83.50% |

- Experiment: `projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/16128580928550013466/alphaEvolveExperiments/7274108873648435913`
- Outcomes: 1 improved, 3 incomplete, 7 invalid
- Quarantined source: [`results/2026-08-17-naive-fast-path-winner.py`](results/2026-08-17-naive-fast-path-winner.py)

### Adversarial reversal

The winner scans arbitrary task keys and string values for the substring
`test`. It therefore routes both `latest_release` and `customer testimony` to
`focused_test`, missing the required answer capability. Adversarial score:
`-102000`; 2 of 4 cases incomplete. This is a hard promotion failure.

## Next promotion work

1. replace free-text substring inference with an explicit normalized task kind;
2. add real, anonymized Straitjacket traces and actual provider/model prices;
3. run adversarial and perturbation suites before using a candidate as a new
   seed;
4. run a matched-model live A/B for completion, visible tokens, turns, dollars,
   wall time, and omissions;
5. promote only through a reviewed source change after all gates pass.

## Reviewed runtime integration

The validated architectural lesson, not the rejected generated source, was
integrated into `ctx.orchestrator.orchestrate` and `fallback_route`.
High-confidence answer, inspect, named-test, diff-review, and small-edit tasks
skip the coordinator and compile to one- or two-node DAGs; uncertain tasks
retain coordination and the four-node general fallback. Runtime
tests preserve verification for mutations and reproduce the `latest` and
`testimony` adversarial cases. Default local route estimates were `$0.0070` for
a named test, `$0.0172` for a one-line verified edit, and `$0.6079` for the
unchanged complex fallback. Live matched-model measurement is still required.

## Receipt-informed iteration — 2026-08-18

Route execution now emits prompt-free `ctx.route-run/v1` receipts and keeps
semantic success in a separate explicit label ledger. A deterministic exporter
freezes only structural task fields, host/model identity, bounded measurements,
and evidence category into the route-replay corpus.

The first matched named-test observation found:

| Route | Explicit result | Duration | Estimated spend |
|---|---:|---:|---:|
| interactive `antigravity/gemini-3.5-flash-lite` with escalation | failed | 5.00 s | $0.04375 |
| unattended `claude/claude-haiku-4.5` | named test passed | 10.68 s | $0.03250 |

This evidence changed production routing: hosts declared non-unattended are no
longer automatically selected for assignment, escalation, or coordination.
Explicit pins retain attended use. A separate review probe was manually stopped
without a semantic result and was not mislabeled or admitted to the corpus.

The new route-replay seed scored `170.02963`. Across its search mix it preserved
all completion/admissibility gates while reducing the broad-route baseline by
76.85% estimated dollars, 73.02% visible tokens, 58.33% model turns, 66.67%
tool calls, and 69.26% modeled latency. These are mixed live/contract evaluator
estimates, not billing claims.

- Experiment: `projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/6234818426344204551/alphaEvolveExperiments/16702348582778723064`
- Bound: one seed plus 11 evolved candidates, concurrency 2
- Best evolved candidate: `15703814734136361274`, tied at `170.02963`
- Next admissible candidate: `125.82109`; all others failed completion or the
  restricted evaluator
- Tied candidate: holdout `151.64219`, adversarial `100.0`, all cases complete
- Decision: no generated policy promotion because there was no score improvement

The search has reached a data frontier, not proof of global optimality. The next
iteration should gather labeled live answer, inspect, review, small-edit, and
ambiguous-task runs, then refresh the frozen snapshot before spending on another
campaign.

## Live corpus iteration 2 — 2026-08-18

The corpus expanded from 2 to 12 explicitly labeled observations covering
answer, inspect, review, named test, small edit, and general feature routes.
Read-only cases were reviewed through their stored checkpoints; mutations ran
only in disposable Git fixtures and were labeled from exact diffs plus focused
pytest acceptance checks.

### Completion defects found

1. A coordinator-authored host pin bypassed the automatic unattended filter and
   sent general work to interactive `agy`. Both nodes exited zero after an
   auto-denied permission prompt, produced no files, and were previously shown
   as `2/2` successful.
2. Mutation plans could combine implementation and claimed tests without a
   downstream verification node.
3. Hosts could exit zero while explicitly reporting a read-only block or
   `task is NOT COMPLETE`; route status still said `ok`.

The reviewed repair makes coordinator pins advisory across the unattended gate,
requires downstream verification for every mutation node, and converts bounded
explicit failure reports into execution failures eligible for escalation.
Interactive pins remain available only through the explicit
`allow_interactive_pins` API boundary.

### Matched general-feature A/B

The same fully specified `slugify` task ran in fresh disposable fixtures:

| Route | Acceptance | Duration | Estimated spend | Estimated visible context | Model turns |
|---|---:|---:|---:|---:|---:|
| four-stage with frontier plan | 5/5 passed | 202.69 s | $0.7915 | 125,500 | 4 |
| three-stage, Claude/Sonnet implementer | 8/8 passed | 95.79 s | $0.2340 | 70,000 | 3 |
| three-stage, Codex/Terra implementer | failed, no changes | 80.31 s | $0.2180 | 70,000 | 3 |

The passing lean arm reduced estimated spend by 70.4%, estimated visible context
by 44.2%, model turns by 25%, and wall time by 52.7%. A second underspecified
feature correctly exposed the boundary by asking a clarification and making no
changes; when rewritten with an explicit `must` contract, the lean Claude route
passed 6/6 tests in 65.69 seconds.

### Managed campaign 2

- Experiment: `projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/9227756159415260161/alphaEvolveExperiments/6043072145866495514`
- Bound: one seed plus 11 evolved candidates, concurrency 2
- Seed/best managed score: `150.18419`
- Outcomes: 5 ties, 5 incomplete policies, 1 invalid; no managed improvement

AlphaEvolve did not synthesize the four-signal policy within the fixed budget.
The independently reviewed policy—named target, named acceptance, explicit
behavioral contract, and no high-risk scope marker—scored `156.45698` locally,
up 6.27279 points or 4.18% over the campaign seed. It passed the search,
holdout, and adversarial gates and is now the next production/replay seed.
Within the search mix it reduced estimated dollars a further 19.8%, visible
tokens 11.0%, model turns 5.6%, tool calls 7.1%, and modeled latency 16.4%
relative to the previous seed.
