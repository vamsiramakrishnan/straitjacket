# The task ledger — how harnesses collaborate without talking to each other

<sub><a href="README.md">« straitjacket / docs</a></sub>

> **Status: Shipped** (v0.35.0) — [`src/ctx/taskledger.py`](../src/ctx/taskledger.py),
> [`src/ctx/steward.py`](../src/ctx/steward.py),
> [`src/ctx/recovery_policy.py`](../src/ctx/recovery_policy.py); wired into
> [`src/ctx/orchestrator.py`](../src/ctx/orchestrator.py). Covered by
> [`tests/test_taskledger.py`](../tests/test_taskledger.py),
> [`tests/test_steward.py`](../tests/test_steward.py),
> [`tests/test_task_ledger_orchestration.py`](../tests/test_task_ledger_orchestration.py).
> Measured in [`evals/task-ledger-replay-2026-09-02.md`](../evals/task-ledger-replay-2026-09-02.md).
> New to the vocabulary? Read [Routing](ROUTING.md) first.

## What was missing

`ctx orchestrate` already did the hard parts of multi-harness collaboration:
a cheap coordinator emits a DAG, each node is assigned the cheapest
`(harness, model)` that clears its capability tier, the plan is priced before
any spend, and results cross between nodes as `checkpoint:` addresses rather
than raw bytes. Four evolved policies (handoff, mutation isolation,
verification, wave concurrency) shaped each decision.

What it lacked was the layer the meta-harness field calls a *server* or a
*daemon*: stateful policy that sits **between** harnesses and outlives any one
of them. Concretely:

| | before |
|---|---|
| a node that did not finish | escalated one tier up, once, whatever the reason |
| the budget | checked against an *estimate*, per wave |
| turns per node | not observed at all |
| a node telling another something | impossible — nodes spoke to the orchestrator via stdout, and to nothing else |
| the orchestrator process dying | the run was gone; checkpoints survived, the DAG's state did not |

A fifth policy — `choose_recovery`, typed retry / escalate / re-plan / honest
stop — had been evolved in the AlphaEvolve portfolio and registered as a
canary lever with its seam at `run_route`, and never wired in.

## The one design decision

**Harnesses never talk to each other. They talk to the ledger.**

This is the rule already applied to transcripts (ADR 001: the transcript is an
index, the store is the truth), applied to orchestration. The ledger is an
append-only JSONL file per task under the workspace's bookkeeping directory
(`.ctx-session-reads/tasks/<task-id>.jsonl`, gitignored). Every harness reads
from it and appends to it; the orchestrator is one more reader. Durability
then costs nothing — the ledger *is* the run — and resuming is a replay.

```
  BEFORE                                     AFTER

  ctx orchestrate (one process)              ┌──────────────────────────────────────┐
     │                                       │  .ctx-session-reads/tasks/<id>.jsonl │
     ├─ spawn claude ──stdout──▶ checkpoint   │  task · claim · handback · steward    │
     ├─ spawn codex  ──stdout──▶ checkpoint   │  verdict · inbox                     │
     └─ spawn agy    ──stdout──▶ checkpoint   └───▲──────▲──────▲──────▲─────────────┘
                                                 │      │      │      │
  process dies → run is gone               ┌─────┴┐ ┌───┴──┐ ┌─┴───┐ ┌┴────────┐
  a node cannot hand anything back         │claude│ │codex │ │ agy │ │ steward │
                                           └──────┘ └──────┘ └─────┘ │ (no LLM)│
                                           read inbox · write handback└─────────┘
```

## The six rows

Every row is a closed set of fields. Wherever content is involved, the row
carries an **address** and the content lives in the store.

```
ctx.task/v1       the task: goal as a blob: ref, the assigned DAG, the budget
ctx.claim/v1      "node N, attempt A: I am <host/model>, expect ~T turns, ~$C"
ctx.handback/v1   "node N, attempt A: stopping — <reason>, <failure_kind>,
                   checkpoint:…, T turns, $C, exit E"
ctx.steward/v1    the steward's decision on that handback, and the budget it saw
ctx.verdict/v1    a verification result and its evidence address
ctx.inbox/v1      one node → another: an address, plus a bounded note
```

A coordinator re-plan that adds nodes appends a second `ctx.task/v1` row
(`source: replan`) carrying only the nodes it added, so a resume folds every
task row and the added nodes are as much a part of the route as the originals.

`reason` is one of `done · failed · blocked · over_budget · over_turns ·
low_confidence · prewalk_handoff`. `failure_kind` is the vocabulary the
recovery policy was evolved against: `auth_failure · safety_denied ·
permission_denied · rate_limited · transient_transport · capability_limit ·
incomplete_contract · repeated_incomplete · verification_failure ·
missing_evidence · context_omission · stalled · wall_timeout · unknown`,
plus `none`.

A handback is the row that turns collaboration into a loop. A node used to have
two exits; it now has seven, and each is a typed input to a policy rather than
a crash to route around. `prewalk_handoff` is the one exit that is not a
failure at all — a frontier model succeeded at a narrower goal (plan, then one
edit) by design and handed off on purpose; the steward routes it to its own
`handoff_cheap` action rather than the failure-recovery policy
([Prewalk](PREWALK.md)).

## The lifecycle of one node

```
   ready ─▶ claim (host/model, ~turns, ~$) ─▶ launch ─▶ handback
                                                          │
             ┌──────────┬───────────┬──────────┬──────────┼────────────┐
             ▼          ▼           ▼          ▼          ▼            ▼
           done      failed     blocked   over_budget  over_turns  low_confidence
             │          │           │          │          │            │
        checkpoint ─────┴───────────┴────── steward ──────┴────────────┘
        to dependents                          │
                                    ┌──────────┼──────────┬─────────────┐
                                    ▼          ▼          ▼             ▼
                               retry_same  escalate    replan     stop_blocked /
                               (same       (cheapest   (leave for  stop_budget
                                model)     one tier    the coord-  (node ends,
                                           up)         inator)     honestly)
```

The steward is not a model. It is two pure functions over ledger state:

- `classify_failure` maps what the host actually returned — exit code, output,
  turns, attempt number — to a `(reason, failure_kind)`.
- `decide` builds the menu of actions that **exist for this node right now**
  (escalate needs an installed stronger model; re-plan needs a coordinator with
  re-plans left; a node at `max_attempts` is offered nothing that consumes an
  attempt), each with a real cost, and asks `choose_recovery` which to take.

The decision is written to the ledger **before** it is acted on, so every
escalation, retry and stop is in the receipt with the reason it was chosen.

## The four axes are one loop

Cost, quality, turns and complexity are not four knobs. They are a gate, a
budget and two signals:

```
   complexity ──sets──▶ ┌─────────────────┐ ◀── quality floor
   (coordinator's       │   TIER GATE     │     (acceptance tests, risk)
    estimate)           │ min tier for N  │
                        └────────┬────────┘
                                 │ survivors, ranked by
                                 ▼
   $ remaining ──────▶  ┌─────────────────┐   refuses the CLAIM when the
   (ledger actuals      │     PRICE       │   node's estimate exceeds it —
    minus open claims)  └────────┬────────┘   never starts unpayable work
                                 │ runs; emits
                                 ▼
                        ┌─────────────────┐
                        │ turns consumed  │──▶ past the claim? → over_turns
                        └─────────────────┘      "complexity was wrong"
                                                        │
                                                        ▼
                                              steward re-gates: a stronger
                                              model, or an honest stop —
                                              never the same model again
```

A claim reserves what it expects to cost until its handback, and the check
and the claim are one step under the ledger lock. Two nodes of the same wave
claiming in parallel therefore each see the other's reservation: with budget
for one of them, exactly one launches. What a wave can still overshoot is the
gap between a launched node's estimate and its bill.

Turns are the **feedback signal**, not a limit. A node past its claimed turn
count has told us the coordinator underestimated it, and that becomes a
capability-class failure kind. `[orchestrate] expected_turns` sets the claim;
`turn_ceiling` > 0 additionally hard-bounds Claude nodes at launch
(`--max-turns`). Other hosts expose no equivalent and are bounded by
observation only.

Time has two bounds, kept apart the way headlong's shellm keeps them:
`[orchestrate] node_timeout` is the wall clock, and `idle_timeout` (seconds,
0 = off) is the inactivity bound. Every byte a host writes on either stream
is the beacon — read raw, so a progress character counts — and a node
silent for `idle_timeout` is killed with its process group and handed back
as `stalled`; a node still emitting when `node_timeout` runs out is
`wall_timeout`. They recover differently, because they mean different
things: a stalled node is a stuck model, so the policy escalates it and
never re-runs the same model blind; a wall-timeout node was too big, so the
coordinator gets first say (it can split it) and the same model continues
in the same worktree only when nobody can re-plan. Before v0.38.0 both were
`transient_transport` — the improve route's first live run filed an hour of
harvest work as a transport blip. Claude nodes stream their events
(`--output-format stream-json`) only while the idle bound is on; Codex's
`exec --json` already streams.

## Two classifier calls worth knowing

Both are pinned by tests, because both decide whether a stronger model gets
spent.

- **A one-shot host's own execution denial escalates.** `permission
  auto-denied` and `read-only workspace` mean *this* session, in *this* mode,
  could not act. Another host routinely succeeds, and the orchestrator's
  acceptance test has always required that. So these are `capability_limit`,
  not `permission_denied`.
- **`auth_failure` stops.** Not logged in, unauthorized, expired, invalid key —
  a stronger model fixes none of it. The evolved policy stops rather than
  spends, which the fixed rule could not express. The receipt says
  `stop_blocked`, never `stop_budget`, when money was not the reason.

## Resume

```console
$ ctx orchestrate "add a caching layer"
…
task: task-18d1583556af4e76
resume: ctx orchestrate --resume task-18d1583556af4e76    ← printed when any node did not finish

$ ctx orchestrate --resume task-18d1583556af4e76
resumed task-18d1583556af4e76 from the task ledger; coordinator skipped
  explore    → claude/claude-haiku-4.5 [ok]
     resumed from ledger
  implement  → …
```

Resume folds the ledger: nodes with a `done` handback are restored (status,
checkpoint, the handoff document their dependents read) and never re-run;
nodes that were claimed but never handed back — the process died mid-launch —
run again, which is the direction that costs money rather than truth. The plan
is rebuilt from the `ctx.task/v1` row with the recorded assignment as a pin per
node; a pinned host no longer installed falls back to the cheapest model that
clears the tier, the same rule a fresh plan follows.

## The inbox

```console
$ ctx task send task-18d… implement repo:src/auth.py --lines 40:52@07407f1c --note "start here"
sent to implement: repo:src/auth.py --lines 40:52@07407f1c
```

An inbox row is the `rig send` of this system, with one rule: it carries an
**address**, never content. The ledger enforces the rule: the ref must parse
under the reference grammar (`repo:`, `checkpoint:`, `run:`, `blob:` …), may
be followed only by `ctx get` options, is bounded to 256 characters, and is
refused before it is stored otherwise. The receiving node sees it in its
prompt and resolves it with `ctx get`. Content-anchored addresses
([ANCHORS](ANCHORS.md)) are what make this safe across a file another node is
editing. The same verbs exist on the MCP surface (`task`, `inbox`, `send`), so
an agent inside any harness can read its inbox or hand an address forward
without shelling out.

## Privacy

The route receipt is the export-safe artifact and carries no task text. The
ledger holds to the same rule: the goal is a `blob:` address, node output is
behind a `checkpoint:`, every reason and kind is closed-vocabulary, and the
only free text — an inbox note — is bounded to 200 sanitized characters and
declared as such. `tests/test_task_ledger_orchestration.py` pins that the task
text never reaches the file.

## What the receipt shows

From [`evals/task-ledger-replay-2026-09-02.md`](../evals/task-ledger-replay-2026-09-02.md),
model-free:

- **Resume**: a four-node route killed at each of its four launches and
  resumed re-ran only the unfinished nodes — 6 of the 16 launches a naive
  restart makes were saved, and no node ever ran twice.
- **Typed recovery**: over the seven failure shapes the policy knows, the
  ledger spent 43% less than the fixed escalate-once rule. The saving is
  exactly the two honest stops (auth, safety) and the two retries (transient,
  rate-limited) the fixed rule turned into tier-ups.
- **Budget**: with an estimate that fit the budget and bills that did not, the
  claim check refused the node it could not pay for and spend stayed inside
  the line; the estimate-only loop would have run everything.

## Known limits

- **`low_confidence` and `verdict` rows have writers but no automatic
  producer yet.** The vocabulary is reserved and validated; a node can emit
  them through the ledger API, and `ctx task show` renders them, but no host
  adapter infers confidence from output today. That is the next lever, and it
  needs a measured signal, not a heuristic.
- **Turn counts come from hosts that report them.** Claude (`num_turns`) and
  Codex (`turn.completed` events) do; the Antigravity SDK does not, so its
  nodes are never `over_turns`.
- **The beacon is host output, nothing finer.** A node that runs one long,
  silent command (a twenty-minute test suite inside a single tool call)
  looks stalled to the idle bound even though it is working, because the
  host emits its next event only when the tool returns. Set `idle_timeout`
  above the longest single command a node is expected to run, or leave it
  at 0. Reading the workspace ledger's own writes as a second beacon is the
  next step, not this one.
- **Wave-parallel nodes can jointly overshoot.** The claim check runs per
  node; two nodes launched in the same wave each pass it against the same
  remaining budget. The receipt shows the per-wave check catching the
  overshoot on the next wave. Serialising claims within a wave is a policy
  choice (`wave_policy`) the ledger does not make on its own.
- **No topology layer.** OpenRig-style pods, edges and culture files are a
  static description that *produces* tasks and claims on this ledger; they are
  not built here. The ledger is the protocol such a layer would write to.
