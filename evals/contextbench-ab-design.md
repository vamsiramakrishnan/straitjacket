# Testing ctx effectiveness on ContextBench: the design, registered in advance

**Status** stage 0 built and validated · stages 1 and 2 not run · predictions
below are recorded **before** any paid arm, deliberately.

## Why the first attempt could not answer the question

[`contextbench.py`](contextbench.py) scores a deterministic retrieval policy in
one pass. Instrumenting it showed the policy, not ctx, was the binding
constraint: `ctx def` contributed 6 of 1,416 retrieved blocks and the rest came
from the runner's own regex fallback. The
[receipt](contextbench-2026-09-10.md) carries the retraction in full.

The deeper problem is the unit. ContextBench is a **process** benchmark: it
asks what an agent looked at while resolving an issue. A policy scored in one
pass is a different object, and no amount of tuning that policy makes it the
right one. Worse, tuning it would optimise the stand-in for the model.

"Is ctx effective" is also not a question about ctx alone. It only means
something against a counterfactual: the same agent, same task, **without** ctx.

## Stage 0 — the free half, built

Two pieces, both model-free, both usable today.

**An arm-agnostic trajectory extractor** (`src/ctx/trajectory.py`). It
reconstructs the file regions an agent actually opened, from a recorded
transcript and nothing else. It reads no ctx state and does not require ctx to
have been installed, because the moment the ctx arm is measured through ctx's
own telemetry and the native arm by parsing a transcript, the comparison is
decided before it runs. Wherever a result renders line coordinates those are
the evidence: `Read` writes `123→`, `ctx get` writes `L123:`, `grep -n` writes
`path:123:`. Three dialects, one fact.

**Gold-scored replay** (`ctx replay --gold`). `ctx.replay`'s regret oracle says
of itself that it is "a *lower* bound … since the trajectory only proves a
subset of what was needed". ContextBench gold is the missing upper half: what a
human said *was* needed. `BENCHMARK.md` reserved exactly this upgrade.

```bash
python evals/contextbench.py --workdir /scratch/cb --limit 40 --stratify \
    --emit-gold /scratch/gold
ctx replay --gold /scratch/gold/<instance>.json ~/.claude/projects/*/<session>.jsonl
```

Validated against a real recorded session with a deliberate control: three gold
regions the session demonstrably opened scored `retrieved` at full coverage, and
a fourth region in a file it never touched scored `missed`. Its own defect
counter fell from 249 unattributed calls to 10 as three real dialect gaps were
closed, and the 10 that remain are pattern-range `sed`, which cannot be placed
without reading the repository — something the extractor must not do.

## Stage 1 — one arm, to prove the pipeline

A dozen instances, one arm, one repeat. The output is not a number worth
quoting; it is the answer to "does an agent driven over these tasks produce a
trajectory this extractor can read, and does the gold resolve against the tree
at `base_commit`". Cheap, and it fails fast if the plumbing is wrong.

## Stage 2 — the referee

| arm | the agent has | isolates |
|---|---|---|
| **A · native** | its own grep and read | the baseline |
| **B · capture** | ctx containment, no code verbs | does bounding output help |
| **C · capture + verbs** | plus map, search, def, refs, graph | do the verbs earn their place |

Matched model, matched task, matched turn and wall budgets, matched repo state.
Twenty instances, three repeats, median aggregation — the charter's existing
discipline, because temperature and seed are not controllable through a host.

**B against C is the split the first attempt could not make at all**, and it is
the one that separates "containment works" from "retrieval verbs help".

### What gets measured

- **Context recall** at file, block and line. Did the agent reach the gold?
- **Evidence density**: gold lines seen per model-visible token.
- **Gold regret**: visible tokens minus the tokens of the gold context itself.
- **Explored versus utilized**: files read against files edited. The paper's own
  finding is that these diverge; the extractor reports both.
- **Resolve parity** as the gate. The charter is explicit that no headline is
  reportable unless solved-under-ctx / solved-native stays near 1.0.

## The prediction, recorded before the run

Written down now so the result cannot be narrated afterwards into a win.

1. **Recall moves little.** A competent model with grep also finds the right
   files. If ctx's headline were recall, the honest expectation is parity.
2. **Density and regret move a lot.** Containment changes the denominator
   without touching the numerator, and that is invisible to recall alone: a
   native arm can reach the same gold and pay several times over for it.
3. **C beats B on dispersed gold, not on single-file gold.** The verbs exist to
   cross a call edge; where the answer is in one file, search alone suffices.
4. **The probe-starved third is where an agent should dominate.** Fourteen of
   forty instances carry no code-shaped identifier. The deterministic policy
   scored 0.061 file F1 on them. If arms A and C both handle them, that confirms
   the stand-in was the constraint all along.

**Falsification.** If recall drops materially under B or C, that is a real
result against ctx and gets published as one. If density does not improve, the
containment claim is not supported on this corpus and the charter's
"receipts before doctrine" rule applies to us.

## Two traps that would invalidate the whole thing

**Instrumentation asymmetry.** Measuring one arm through ctx telemetry and
another by parsing a transcript builds the answer into the tool. This is why
stage 0 is a single extractor with a test that a native trajectory and a ctx
trajectory over identical regions score identically.

**A parity gate that quietly collapses to Python.** Resolve parity needs the
per-repo build toolchain to run `f2p`/`p2p` tests. Score retrieval across all
eight languages, but gate parity only on the families that actually build —
what `swe_learn.py` already bootstraps — and say which those are.

## What not to do

Do not tune the deterministic policy to raise its score. That optimises a
stand-in for the model. Its one honest remaining use is as a zero-cost
regression gate when a verb's ranking changes, and
[`contextbench.py`](contextbench.py) now shares its metric definition with the
trajectory scorer so the two cannot drift apart while doing it.
