# The decision rule: is `ctx agent` worth keeping? (2026-09-16)

The [uncapped sweep](deepswe-2026-09-13.md) ran five arms once each and left
the question open: the `ctx agent` runtime was cheaper per turn but not per
task, at n=1, with nothing resolved. One run cannot tell a real effect from a
coin flip, so this is the rematch that was supposed to settle it — two arms,
the same eight DeepSWE tasks, **two repeats each**, no turn cap, DeepSWE's own
three-hour per-task budget, haiku.

The two arms are the ones the earlier sweep left standing:

- **`naive`** — `claude -p` with the host's own tools, no harness.
- **`sdk_nopack`** — the `ctx agent` runtime on the Claude Agent SDK: ctx's
  retrieval tools instead of Grep/Glob, the Bash router that refuses a shell
  grep and names the `search` call to make instead, and no turn-one context
  pack.

32 sessions, no session errors, no verifier timeouts, $33.20.

## Result

Totals across all 16 cells per arm:

| arm | resolved | fail-to-pass earned | cost | turns | output tokens |
|---|---:|---:|---:|---:|---:|
| naive | 0/16 | 605/1,028 | $17.06 | 1,425 | 601,976 |
| `sdk_nopack` | 0/16 | **694**/1,028 | **$16.14** | 1,601 | 649,204 |

Taking the median of each task's two repeats first, which is the charter's
rule for a live arm:

| arm | fail-to-pass | cost | turns | cost per test | cost per turn |
|---|---:|---:|---:|---:|---:|
| naive | 302 | $8.53 | 712 | $0.0282 | $0.0120 |
| `sdk_nopack` | **347** | **$8.07** | 800 | **$0.0233** | **$0.0101** |

The runtime earns more test credit for less money. It wins 6 of the 8 tasks
on median fail-to-pass, spends 12% more turns, and each of those turns is
16% cheaper, because ctx's retrieval answers in bounded digests where the
naive arm pages whole files into the window.

Pooling with the earlier uncapped run gives three independent observations,
all pointing the same way:

| run | naive f2p | `sdk_nopack` f2p |
|---|---:|---:|
| uncapped, 1 repeat (2026-09-13) | 294 | 367 |
| decision rule, repeat 1 | 302 | 351 |
| decision rule, repeat 2 | 303 | 343 |

## What the variance says, and it is the important part

The per-task difference between arms is **smaller than the difference between
two runs of the same arm on the same task**.

| | median repeat-to-repeat spread | worst |
|---|---:|---:|
| naive | 9 tests | 42 |
| `sdk_nopack` | 16 tests | 33 |

Against that, the per-task arm difference (median of repeats, `sdk_nopack`
minus naive) is `+6, -7, +31, +1, +5, +2, -9, +15`: mean +5.6, median +3.5.
One task swung 42 tests between two identical naive runs — more than the
entire arm effect on seven of the eight tasks.

So the honest statement is: **the direction is consistent across three runs
and eight tasks, the magnitude is not established.** Six of eight tasks
favouring one arm is a sign test at p≈0.29; it is not significance, it is a
lean. What the three runs do establish is that the runtime is not *worse*,
which is what the n=1 result had left genuinely uncertain.

Neither arm resolved a single task. Every number here is partial test credit
on tasks that haiku does not finish, which is the regime this model runs in
and the reason resolution rate is useless as a signal at this size.

## The decision

**Keep the runtime, do not scale the claim.** The cost case is the one that
holds up: cheaper per turn, cheaper per test, three runs running, and it costs
nothing to keep a mechanism that is already built, tested and documented. The
quality case is a lean, and the receipt says lean, not win.

What would settle it is more repeats, not more arms — the variance above says
a fourth and fifth repeat buys more than a sixth arm ever would. That is worth
roughly $17 per repeat and is not scheduled.

## Reproduce

```bash
AGENTBENCH_CTX=/path/to/ctx python3 evals/agentbench/harness.py \
  --adapter deepswe --arms naive sdk_nopack --model haiku \
  --max-turns 0 --session-timeout 10800 --jobs 6 --repeats 2 \
  --adapter-arg ids=adaptix-name-mapping-aliases,bandit-incremental-cache-control,\
bandit-interprocedural-taint-checks,cattrs-partial-structuring-recovery,\
ipython-session-bundle-replay,kombu-single-active-consumer-priority,\
mashumaro-flattened-dataclass-fields,mobly-grouped-test-barriers \
  --resume out/deepswe.partial.json --out out
```

`--resume` naming a file that does not exist yet is how a relaunch after a
container restart picks up the finished cells; it resumes nothing on the
first launch rather than failing.

Machine record: [`results/deepswe-decision.json`](results/deepswe-decision.json).
