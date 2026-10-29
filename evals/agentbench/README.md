# agentbench — plain Claude versus the wrapper bundle

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

# sj — same task invocation, with the full ctx wrapper intervention
ctx wrap claude --proxy -- -p "<task>" --max-turns 40 --allowedTools "..."
```

The task prompt, fixture, requested tool list, and turn cap match. The effective
tools and prompt do not: `ctx wrap` can inject guidance, expose ctx tools, proxy
traffic, and, with the default collapse policy, disallow native `Grep` and
`Glob`. Model parity is auditable only when `--model` is supplied; `model: null`
means the host default was used but not recorded. Fixtures carry `ctx.toml` and
git for both arms so the tree shape is identical.

Arm construction follows `evals/spec3_runner.py` (the frozen referee) so numbers
from the two harnesses stay comparable. Two competitor arms are wired for
contrast, each run with vendor defaults: `headroom` launches Claude Code through
[headroom-ai](https://pypi.org/project/headroom-ai/)'s compression proxy
(`pip install "headroom-ai[proxy]"`; set `AGENTBENCH_HEADROOM` to a venv
binary), and `maki` runs [maki.sh](https://maki.sh), a different agent whose
`--print` mode is a drop-in for Claude Code's JSON output, on the same model
(`AGENTBENCH_MAKI` for the binary; needs `ANTHROPIC_API_KEY`, since it is not
Claude Code and cannot use its login). Every Claude Code session's first-request
prefix (tool count, catalogue bytes, deferral) is recorded per run, so a
wrapper that inflates the host prompt shows up in the report, not just in cost.

Two more arms make ctx the host rather than a wrapper: `sdk` runs
`ctx agent -p` (the Claude Agent SDK driving the same `claude` binary with a
lean built-in surface, ctx's retrieval verbs as in-process tools, the wrapper's
hooks, with `--pack` a `ctx pack` in the first turn; `pip install
'ctx-harness[agent]'`, `AGENTBENCH_CTX` names the `ctx` of an environment that
has it) and `sdk_nopack` is the runtime's default, without the pack.

`--max-turns 0` lifts the turn cap (the session runs until the agent stops;
`--session-timeout` is the wall-clock budget, 10800 s being DeepSWE's own), and
`--resume PARTIAL.json` seeds the sessions a killed sweep had finished.

`pack_recall.py` is the model-free referee for the pack itself: for each
validated DeepSWE task it ranks the checkout with `ctx pack` and scores recall
of the source files the reference solution changes, against a keyword-count
baseline (`results/pack_recall.json`; docs/CODE-SEARCH.md).

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

### `deepswe` — DeepSWE v1.1, graded by the task's own verifier

[DeepSWE](https://github.com/datacurve-ai/deep-swe) is 113 original,
long-horizon feature tasks on active repositories (TypeScript, Go, Python,
JavaScript, Rust), each with a held-out program verifier. v1.1 grades **only
committed work**, extracted as `git diff --binary <base> HEAD`, re-applied to a
pristine checkout in a separate environment, with the held-out `test.patch`
applied afterwards and a whitelist of test ids scored from JUnit/CTRF. Editing
tests cannot help; leaving edits uncommitted loses them.

The adapter reproduces that pipeline without docker: the image's `RUN`/`ENV`
lines are replayed into a per-run virtualenv, the agent gets that venv on
`PATH`, and grading runs the task's own `tests/test.sh` + `grader.py` against a
pristine checkout and a pristine copy of the venv snapshotted before the
session. Python tasks only (the other language images need toolchain steps the
replay does not translate); tasks whose image needs `apt-get` fail to build
here and are excluded by validation rather than silently scored 0.

```bash
python evals/agentbench/validate.py --adapter deepswe --jobs 6          # model-free, all 34 python tasks
python evals/agentbench/harness.py --adapter deepswe --model haiku --max-turns 60 --jobs 4 \
    --arms naive sj --adapter-arg ids=cattrs-partial-structuring-recovery,httpx-streaming-json-iteration
```

`validate.py` uses the adapter's own controls, because DeepSWE's cheat
surface differs from SWE-bench's: `baseline` (no patch), `gold` (reference
patch, committed like a submission), `tampered` (**no fix**, every held-out
test file rewritten to pass trivially — must score 0, which proves the grader
resets them), and `vandal` (gold applied, then the solution's source files
replaced with `raise` — must score 0, which proves tests actually execute).

Two deviations from the official runner are deliberate and recorded in the
results: the agent's network is not cut (the task's `no-network` mode cannot
be enforced outside a container), and the interpreter is a uv-managed CPython
3.12 (`DEEPSWE_PYTHON` overrides) rather than the image's own build. The
corpus commit is stored on every task record.

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

The model-free canary referee passes 12/12, the SWE-bench adapter loads real
instances with real test lists, and `report.py` refuses simulated payloads.
Three live receipts are committed: the three-task canary and the one-task
dogfood mission (host-default model, unrecorded), and the DeepSWE v1.1 sweep
(eight Python tasks, haiku, one repeat, run twice: before and after the two
wrapper fixes it found), plus a single-task iteration loop under
`results/iterations/` (cattrs, four arms including headroom-ai, one change per
iteration) that found the print-mode tool diet and the Read-cap widening bug. The DeepSWE referee was proven on 14 of 34 Python
tasks by `validate.py`; see [`deepswe-2026-09-13.md`](deepswe-2026-09-13.md)
for the mechanism analysis, the exclusion list, and the per-task tables.
[`deepswe-decision-2026-09-16.md`](deepswe-decision-2026-09-16.md) is the
two-repeat rematch that the uncapped sweep's n=1 result called for: naive vs
the `ctx agent` runtime, and the measurement of how much of the gap is noise.
All are diagnostics, not a broad benchmark or a containment-only ablation. A paid
SWE-bench sweep has not been run and still needs a machine with Claude
credentials and Docker.
