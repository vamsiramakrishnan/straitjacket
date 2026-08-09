# agentbench — the wrapper is the only variable

`evals/tokenomics/` drives a **fixed model ladder**: a script calls an API, runs
a subprocess, calls an API again. Nothing in that loop can decide to run a
command or follow an address, so it measures a digest formatter. That eval's own
result made the limit concrete — the bare digest arm trailed until a single
automatic `ctx get` was added, at which point it matched the paid channel for
$0.00. The consumer, not the digest, was the bottleneck.

This harness removes that bottleneck by putting a **real agent** in the loop:

```bash
# naive
claude -p "<task>" --max-turns 40 --allowedTools "Bash Read Grep Glob Edit Write"

# sj — identical, one prefix
ctx wrap claude --proxy -- -p "<task>" --max-turns 40 --allowedTools "..."
```

Same model, same fixture, same prompt, same tools, same turn cap. Fixtures carry
`ctx.toml` and git for **both** arms so the tree shape is identical. The agent
runs the noisy suite itself, floods itself, and retrieves itself.

Arm construction follows `evals/spec3_runner.py` (the frozen referee) so numbers
from the two harnesses stay comparable. A `headroom` arm is wired for contrast.

## Validate the referee before you spend

An agent benchmark is only as trustworthy as its grader. The suite that started
this work published 80% on SWE-bench Pro from an evaluator that never ran a
test — it string-matched the gold patch. So the referee is proved first, with no
model involved:

```bash
python evals/agentbench/validate.py --adapter canary
```

Four states per fixture, all model-free and deterministic:

| State | Setup | Must |
|---|---|---|
| `baseline` | bug present, nothing done | NOT resolve |
| `gold` | the real fix applied | **resolve** |
| `tampered` | fix applied, tests edited | NOT resolve |
| `vandal` | source replaced with garbage | NOT resolve |

`gold` failing means the grader is too strict and scores real fixes as misses.
`baseline` passing means it is too loose. `tampered` passing means an agent can
win by rewriting the tests. `vandal` passing means nothing is being executed.

Current state: **12/12 on the canary adapter.** Writing it caught two live bugs
in this harness — a `pytest -rA` parse that had `STATUS node` backwards (every
node silently scored as absent → False), and a tamper check that fired on
`__pycache__`. It also caught a bad *fixture*: the `deep` tree was imbalanced at
the root, so the injected bug was unreachable and the task resolved at baseline.
That is three false results the referee would have produced before any money was
spent.

Run it against `swebench` too before a paid sweep — there `gold` failing means
the instance environment is wrong, which is the single most common way SWE-bench
numbers go quietly bad.

## Adapters

An adapter is three functions: `load(n)`, `prepare(task, workdir) -> prompt`,
`grade(task, workdir) -> {resolved, f2p, p2p, tests_tampered}`. Plus
`apply_gold()` so `validate.py` can prove it. **Swapping benchmarks swaps the
adapter, not the harness.**

### `canary` — instrument validation, runs anywhere

Three SWE-shaped fixtures: a real git repo, a real bug, a real failing test, a
gold patch, and the same FAIL_TO_PASS / PASS_TO_PASS split SWE-bench uses. No
docker, no network, seconds to run.

Two fixtures are deliberately noisy (2,500–4,000 lines of chatter around the
failure) so the arms can differ. **`quiet` is the low-output control** that
`evals/BENCHMARK.md` insists on: a suite made only of floods will always flatter
the harness, and the tiny-surgical-task regression that produced graduated
engagement is exactly what a control catches.

### `swebench` — SWE-bench Verified

500 human-filtered instances with a mature public harness and official
per-instance images. Verified rather than Pro deliberately: the Pro copy that
started this work ships `FAIL_TO_PASS: []` and `PASS_TO_PASS: []` on every row,
so no grader could ever have scored it.

```bash
# instance metadata is fetched and cached on first use
python evals/agentbench/harness.py --adapter swebench --n 60 --repeats 3 \
    --arms naive sj --adapter-arg exec=docker
```

`exec=docker` runs the suite in the official image and is the path that gives
comparable numbers. `exec=local` with `--adapter-arg python=<path>` is faster
and needs no docker, but you own the dependencies; a wrong environment shows up
as `p2p` failures at baseline, which `validate.py` reports rather than hides.

### Not yet written

**Terminal-Bench** is the closest fit of all — already agent-in-a-terminal, with
a pluggable agent interface, so the two arms register as two agents rather than
needing a fixture adapter at all. `BENCHMARK.md` specs the 30-task slice chosen
**by evidence shape** (compiler, package manager, docker build, process tables,
JSON, ANSI noise, mixed streams, long-runners), not by topic.

**BigCodeBench** is a weak instrument here and should not be a headline: one
function, no repo, no navigation, tiny outputs — almost nothing to contain. Its
place is the low-output control stratum.

## Measurement caveat: the `sj` arm exercises containment, not navigation

straitjacket has two halves. The **emission gate** contains whatever a tool
returned; the **code verbs** (`ctx map` / `def` / `refs` / `callers` / `impact`)
replace the search entirely, so the flood is never produced. On a navigation-
heavy corpus the second half is the more valuable one — and the `sj` arm barely
reaches it.

The only automatic bridge from grepping to the call graph is
`_navigation_nudge` (`src/ctx/hook.py`), and it is heavily gated. Measured by
driving the hook directly, not by reading the source:

| Scenario | Result |
|---|---|
| `bash grep` for 3 distinct **bare identifiers** | nudge fires on the 3rd |
| native **`Grep` tool**, 5 distinct symbols | **silent** — never fires |
| `bash grep -rn 'def '` (a 96 KB flood here) | **silent** |

Four gates:

1. **It is a nudge, not a route.** Nothing invokes the call graph
   automatically; the agent is handed an advisory string and has to take the
   hint.
2. **It only watches Bash.** The handler early-returns unless the tool name
   contains `bash`/`command`, but Claude Code's *native* `Grep`/`Glob` tools are
   the default path. The PostToolUse matcher **does** include `Grep`, so the
   hook is invoked and then discards the event — matcher and handler disagree.
3. **It requires a bare identifier.** `grep -rn 'def '` has a non-identifier
   pattern and is invisible to the detector, though it is the largest flood in
   this repository.
4. **Threshold 3, fires once per session.** Best case: one advisory line.

So an `sj` number from this adapter understates what the tool can do, and must
not be read as "straitjacket on a navigation task". Reading it as a verdict on
the whole system would repeat the mistake this eval suite was built to correct.

The fix for the eval is a third arm, `sj_verbs`: same wrapper plus one line of
doctrine naming the code verbs — the pattern `evals/ab_eval_live.py` already
uses. That yields the B-vs-C split `BENCHMARK.md` asks for, separating *does
containment work* from *do the retrieval verbs help*, instead of conflating
them. It is the same shape that made `sj_hop` informative in
`evals/tokenomics/`: the digest was not lossy, the consumer simply could not
retrieve.

Gate 2 is separately a defect in the hook rather than a property of the eval.

## What gets reported

Resolve rate is the **gate, not the headline**. Per `BENCHMARK.md`, evidence
preservation — `solved_sj / solved_naive` — must hold at ~1.0, and nothing else
is reportable if it does not. Only then do turns, uncached input, cache hit
rate, cost and wall-clock mean anything: a wrapper that finishes cheaper by
failing more has not saved anything.

```bash
python evals/agentbench/report.py --results evals/agentbench/results
```

`report.py` recomputes every cell from per-run records and **refuses to render
any payload not tagged `provenance: live`**. Repeats collapse by majority per
task before aggregation, because temperature and seed are not controllable
through these hosts — determinism of the judgment comes from paired tasks ×
repeats × median, not from a seed.

## Cost, honestly

Agent sessions are a different order of magnitude from the ladder eval. That one
cost ~$5.63 for 240 task-runs. Here a single 40-turn session on one SWE-bench
instance is roughly $0.30–$2. A 60-task × 2-arm × 3-repeat design is **360
sessions, on the order of $150–500**. Decide the budget before running, not
after — and run `validate.py` first, because a referee that is wrong makes every
one of those dollars noise.

## Status

Validated to the session boundary: arm construction is asserted to differ only
by the wrapper prefix, the canary referee passes 12/12, the SWE-bench adapter
loads real instances with real test lists, and `report.py` renders and refuses
simulated payloads. **Live agent runs have not been executed** — this
environment has no usable docker and no agent credential to spend. Everything
above the session call is proved; the session call itself needs a box with
`claude` credentials and, for SWE-bench, docker.
