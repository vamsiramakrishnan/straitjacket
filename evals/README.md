# Evaluations

This directory is the **evidence** behind every performance and quality claim in
the docs. The project's house rule is *receipts before doctrine*: a mechanism
ships because a named referee measured it, not because the design sounds
plausible. New claims need an eval, not an adjective.

The charter is [`BENCHMARK.md`](BENCHMARK.md). Read it first — it explains why no
single leaderboard can referee a system that changes the agent's information
channel, and the standing principle that **external corpora are teachers, never
referees**.

## How to run an eval

Every runnable script lives directly in `evals/` and has a `__main__` block, so
the invocation is uniform:

```bash
python evals/<script>.py [flags]
```

There are two kinds, and the difference matters for what you need installed.

### Model-free — reproducible, no API key

These exercise the capture/digest/retrieval layer only. No LLM, deterministic,
re-runnable in a review sandbox in seconds. Use these to verify a change didn't
regress containment or evidence preservation.

```bash
pip install -e '.[dev]'
python evals/headroom_needle_v2.py     # needle-survival head-to-head vs Headroom
python evals/field_needle.py           # seven strategies on identical hostile bytes
python evals/coverage_corpus.py        # replay real hostile outputs through stub binaries
python evals/plan_collapse.py          # rounds collapse, byte-stable digest
```

Others in this class: `evalset_collapse.py`, `corpus_scoped_scan.py`,
`scip_precision.py`, `plan_value_selection.py`, `replay_detectors.py`,
`replay_sim.py`, plus the reporting tools `bench_report.py`, `matrix_report.py`,
`ctx_anatomy.py`, `ctx_capture.py`, `ctx_account.py` (which reads a recorded
`wire.jsonl` rather than calling a model). `swe_learn.py` is model-free but
fetches SWE instances and reproduces failures in a venv, so it needs network and
build toolchains.

### Live — needs an agent and an API key

These spawn a real agent subprocess per (scenario, arm, repeat) and drive a full
task. They need the host CLI on `PATH` and the matching API key.

```bash
export ANTHROPIC_API_KEY=...           # for the Claude-driven runners
python evals/spec3_runner.py --repeats N --gates ...
python evals/bench_run.py ...
python evals/matrix_runner.py ...
python evals/ab_eval_live.py ...
```

`antigravity_sdk_eval.py` instead drives Google's Antigravity Agent SDK and
needs `GEMINI_API_KEY`. Because temperature and seed aren't controllable through
these hosts, live determinism comes from paired tasks × repeats × median with
frozen-constant checksums — see [`BENCHMARK.md`](BENCHMARK.md).
`agy_ab_matrix.py` collapses a set of its run directories into one priced
model × scenario matrix.

`coding_suite.py` is the dated five-task naive-vs-straitjacket runner behind
[`coding-suite-2026-07-20.md`](coding-suite-2026-07-20.md). Its committed
record has two repeats per arm and aggregate results rather than raw
transcripts, so use it as regime evidence and rerun it before making a current
performance claim.

Two live runners drive from-scratch web builds graded by headless Chromium
(needs `playwright` and the Chromium under `/opt/pw-browsers`) — see
[`vibecode/README.md`](vibecode/README.md):

```bash
python evals/vibecode/harness.py --task todo          # single-shot build
python evals/vibecode/iterative_harness.py --arm solo --arm orchestrated
```

`iterative_harness.py` is the **iterative** one: it builds an app, then reshapes
it twice mid-build with design-review amendments that reverse part of what was
already built, and grades each phase against the earlier behaviours that must
survive. Its arms are the routing comparison — one frontier model doing
everything, against the orchestrator splitting plan from build across models and
vendors.

## The four instruments

The charter answers four questions with four instruments, because one corpus
can't referee all of them:

| Question | Instrument |
|---|---|
| Does it help a real agent on a real task? | live A/B, same agent both arms |
| Does containment survive hostile outputs? | the coverage corpus (real output families) |
| Does it ever drop the decisive line? | needle-drop + evidence-conformance |
| Are the invariants held? | the Tier-0 `sj_canary` conformance tests |

The load-bearing gate is **evidence preservation ≈ 1.0**: high containment is
only a win at matched-or-better task success, with every omission declared and
resolvable.

## The receipts

The dated `*.md` and `*.json` files are frozen receipts — each names its
fixture/corpus, the baseline and treatment, the constants chosen before the run,
and the results including failures and reversals, not only wins. When you cite a
number in the docs, link its receipt here.

## Adding an eval for a new claim

1. Pick the instrument that answers your claim's question (above).
2. Freeze the fixture/corpus and the constants *before* the run.
3. Prefer a model-free referee when the claim is about the digest/retrieval
   layer — it stays reproducible.
4. Commit the runner and a dated receipt in the same change as the mechanism.

See [`../CONTRIBUTING.md`](../CONTRIBUTING.md) for how this ties into the merge
gate, and [`BENCHMARK.md`](BENCHMARK.md) for the tier mapping.
