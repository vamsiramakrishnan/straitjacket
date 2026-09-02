# Task ledger replay — resume, typed recovery, budget against actuals

**Date:** 2026-09-02 · **Referee:** `evals/task_ledger_replay.py` (model-free,
deterministic, injected hosts) · **Record:** [`task-ledger-replay-2026-09-02.json`](task-ledger-replay-2026-09-02.json)

```bash
python evals/task_ledger_replay.py          # the tables below
python evals/task_ledger_replay.py --json   # the record
```

## The three claims this measures

`ctx orchestrate` gained a task ledger ([TASK-LEDGER](../docs/TASK-LEDGER.md)):
every launch is claimed and handed back with a typed reason, a deterministic
steward decides what a non-finish means, and a killed run resumes. Three things
justify that, and each is a number rather than an argument:

1. resume does not repeat finished work;
2. typed recovery spends less than the fixed escalate-once rule it replaces;
3. a budget checked against actuals stops where the estimate would not.

Model-free by design: a fake host roster (Claude + Codex tiers), injected
launchers that return scripted exits, output and priced usage, a real
`run_route`. Nothing calls a model; the receipt re-runs in a review sandbox in
seconds and cannot drift with a vendor's weights.

## Result

```text
[task ledger replay · model-free]

1. resume — a 4-node route killed at every launch, then resumed
 died after  done before  resume ran  naive restart  all done
          1            0           4              4      True
          2            1           3              4      True
          3            2           2              4      True
          4            3           1              4      True
launches saved by resume: 6 of 16 a naive restart would make (38%); no node ever ran twice

2. typed recovery — one node fails once, then would succeed on any second attempt
failure              classified           steward        done   spent  fixed rule
auth_failure         auth_failure         stop_blocked  False   0.020       0.120
safety_denied        safety_denied        stop_blocked  False   0.020       0.120
rate_limited         rate_limited         retry_same     True   0.040       0.120
transient_transport  transient_transport  retry_same     True   0.040       0.120
capability_limit     capability_limit     escalate       True   0.120       0.120
incomplete_contract  incomplete_contract  escalate       True   0.120       0.120
execution_denied     capability_limit     escalate       True   0.120       0.120
total spend across the corpus: ledger $0.480 vs fixed rule $0.840 (43% less)
the two honest stops (auth, safety) are the fixed rule's wasted escalations; the transient retry is its needless tier-up

3. budget against actuals — the estimate fits the budget; the bills do not
budget $0.260 · estimate $0.200 (fits) · actual $0.117/node
nodes run: 2 of 4 · ledger spend $0.234 (within budget: True) · refused at the claim, never launched: 1
the estimate-only loop would have run all 4: True
```

## Reading it

**Resume.** A four-node route is killed at each of its 4 launches in
turn and resumed. Across the four crash points a naive restart makes 16
launches; resume made 10, saving 6 (38%), and
`max_attempts_any_node` stayed at 1 every time — no finished node ever ran
again. The dying launch itself is not counted as finished, because it never
handed back; that is the safe direction (re-run costs money, never truth).

**Typed recovery.** One economy node fails once in each of 7
scripted ways, and would succeed on any second attempt. The fixed rule this
replaces escalated one tier up on every failure, so it spent an economy attempt
plus a standard one — $0.120 — every time. The ledger spent
$0.480 against the fixed rule's $0.840, 43% less, and the
saving is exactly where the policy's judgement lies:

- **2 honest stops** (auth_failure, safety_denied): a login
  problem and a policy refusal are not capability problems. The fixed rule
  bought a stronger model for both and got nothing; the ledger spent one
  economy attempt and stopped with the reason on record.
- **2 retries on the same model** (rate_limited, transient_transport):
  a transport hiccup and a rate limit are transient. The fixed rule tiered up;
  the ledger retried at economy price and completed.
- **Escalations where escalation is right** (capability limit, incomplete
  contract, and a one-shot host's own execution denial): same action, same
  spend as the fixed rule. The policy is not cheaper by being timid.

Note `execution_denied` is classified as `capability_limit`: a host that
auto-denied its own tool or ran read-only can be cleared by another host, and
the orchestrator's acceptance suite has always required that it escalate.

**Budget against actuals.** The planner refuses a budget the estimate does not
fit, so the budget here is set above the estimate ($0.260 against
$0.200) and every node then bills $0.117, well
over its share. The estimate-only loop would have run all 4. The
ledger ran 2, refused 1 at the claim before any launch,
and finished at $0.234 — inside the budget.

## What this does NOT establish

- **Not a field rate.** The seven failure shapes are the vocabulary the policy
  was evolved on, given equal weight because nothing here knows how often each
  occurs in practice. The 43% is the saving *over this corpus*; the real number
  depends on the real mix, which the ledger now records
  (`ctx.steward/v1` rows) and `route.jsonl` exports.
- **Not task success.** Every scripted second attempt succeeds. This measures
  what the steward *spends* to reach it, not whether real hosts recover.
- **Not wave-parallel budgets.** The claim check runs per node; nodes launched
  in the same wave each pass it against the same remaining budget. The route
  here is a chain, so that limit does not bite; on a fan-out it can, and the
  per-wave check catches it one wave late.
