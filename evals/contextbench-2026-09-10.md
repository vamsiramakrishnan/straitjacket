# ContextBench, first receipt — the search lane against human gold context

**Date** 2026-09-10 · **Runner** `evals/contextbench.py` · **Corpus**
[ContextBench](https://arxiv.org/abs/2602.05892) `contextbench_verified`
(500 instances) · **Policy** `searchlane`, deterministic, no model in the loop.

## Why this run exists

`evals/swe_learn.py` scores the **output** channel: reproduce a real failure,
digest it, ask whether the gold files were surfaced. Its standing finding, from
the first receipts, is that most gold evidence is **not-in-output** — no digest
of a test run could have delivered it, because it lives in source the agent has
to go and find. `BENCHMARK.md` names that residue the search lane's territory
and reserves a slot for a corpus that could referee it, "contingent on
verification" of line-level gold regions.

ContextBench publishes exactly that: human-annotated gold context as
`{file, start_line, end_line}` blocks. Verified against the actual release
before anything was built on it:

| | |
|---|---|
| instances (`contextbench_verified`) | 500 |
| repositories | 58 |
| languages | 8 |
| gold blocks | 4,597 over 1,746 files |
| gold lines | 125,009 |
| median gold blocks / files per instance | 6 / 2 |

## Standing caveat, stated once

**This is a teacher, not a referee.** The numbers below are not comparable to
the paper's agent tables and must never be published as such. Two reasons, both
structural:

1. The paper's evaluation harness is not public. File- and line-level scoring
   here follows its stated definitions; **block-level alignment is an
   adaptation** — a gold block counts as retrieved when a retrieved span covers
   at least half its lines, rather than by AST node alignment.
2. `searchlane` is a **deterministic policy with no model in it**. The paper's
   numbers come from LLM-driven agents over many turns. This measures the floor
   the verbs deliver unaided, which is the number that tells us whether a
   ranking change helped.

What this run produces for us is a **defect queue**, not a score.

## Two measurement bugs, found and fixed before any number was reported

Recording these because both would have produced a confidently wrong result.

**Gold paths are not uniformly repo-relative across the four upstream
sources.** Multi-SWE-bench annotations were produced inside a container and
carry an absolute prefix (`/workspace/facebook__zstd__0.1/programs/fileio.c`);
SWE-bench-Verified rows are already relative. The first run scored 0.00 on
every C instance for this reason alone. The runner now strips the container
prefix, rebinds each gold path against the real tree at `base_commit`, and
reports anything that still will not resolve as **annotation drift** —
excluded from scoring, since a file absent from the tree cannot be retrieved
from it.

**A policy that returns grep hits scores block recall 0.00 by construction.**
Gold is annotated as definition blocks; a single matched line can never cover
half of one. The early runs found the right *files* and scored zero at block
level, which reads as a lane failure and is actually a units mismatch.
Retrieval now expands every hit to its enclosing definition — via `ctx def`
where a probe names the symbol, via a local block scanner otherwise — which is
what an agent reads anyway.

## Results

Two arms, each executed twice — before and after a change to how the graph
stage picks its seeds. Both arms' aggregates came back **identical** across the
pair, which is the determinism the charter asks for: the sample is fixed by
instance id rather than a RNG, and the policy takes no random input.

### Stratified across all eight languages (n=40, `--stratify --limit 40`)

| budget | file F1 | block R | block P | line R | line P | retrieved tok |
|---|---|---|---|---|---|---|
| 2,000 | 0.104 | 0.021 | 0.026 | 0.018 | 0.046 | 1,367 |
| 8,000 | 0.101 | 0.041 | 0.028 | 0.045 | 0.035 | 3,851 |
| 32,000 | 0.100 | 0.051 | 0.031 | 0.058 | 0.033 | 5,737 |

### Python only (n=12, SWE-Bench-Pro-heavy — the hardest tier)

| budget | file F1 | block R | block P | line R | line P | retrieved tok |
|---|---|---|---|---|---|---|
| 2,000 | 0.224 | 0.069 | 0.067 | 0.093 | 0.130 | 1,714 |
| 8,000 | 0.187 | 0.178 | 0.154 | 0.229 | 0.135 | 6,729 |
| 32,000 | 0.180 | 0.222 | 0.112 | 0.281 | 0.086 | 16,223 |

### By language, at the 32k budget (stratified run)

| language | n | file F1 | block recall | line recall |
|---|---|---|---|---|
| python | 6 | 0.238 | 0.204 | 0.250 |
| java | 4 | 0.176 | 0.057 | 0.073 |
| cpp | 3 | 0.162 | 0.067 | 0.064 |
| rust | 4 | 0.099 | 0.017 | 0.045 |
| go | 6 | 0.057 | 0.054 | 0.017 |
| c | 5 | 0.044 | 0.000 | 0.000 |
| typescript | 6 | 0.044 | 0.000 | 0.006 |
| javascript | 6 | 0.026 | 0.000 | 0.001 |

## What the numbers say

**1 · The search lane is Python-shaped, and now we have the size of it.**
Python leads every other language by 4–10× on file F1, and is the only one with
non-trivial block recall outside C++/Go. This was *predicted* by the charter —
item 6 of the build list calls multilingual "the regression corpus for the
Python-shaped mechanisms" — but predicted is not measured, and now it is
measured against human gold. `ctx def` reports `engine ast`; the languages
where that engine is thinnest are precisely the languages at the bottom of this
table. This is the highest-value item the run produced.

**2 · The bottleneck is candidate generation, not packing.** File F1 is
essentially flat across a 16× budget increase (0.104 → 0.100) while block
recall more than doubles (0.021 → 0.051). More budget buys *depth inside files
already found*, never new files. The stratified run's retrieved tokens saturate
at 5,737 against a 32,000 budget — the lane runs out of candidates long before
it runs out of room. Any effort spent tuning the packer is therefore misspent;
the work is in locating.

**3 · A third of the corpus is unreachable without a model.** 14 of 40
instances are **probe-starved**: the issue text carries no path, no traceback
and fewer than two code-shaped identifiers. "Restrict mutable tuple recovery.
This is a fix for #1123." is the whole issue. Those score 0.061 file F1 against
0.121 for probe-anchored instances. This is not a lane defect — it is a
quantified statement of what a model in the loop actually buys here, which is
probe generation, and it is the one place where the agent arm would be
measuring something real.

**4 · The most instructive single failure was not a bug.** On a django instance
the issue named `PropertyGroup`, `property_groups`, `manager_managementagent` —
every one of them the *reporter's own application*, none present in django.
Search returned zero hits. The only true signal was a traceback path, which is
also a gold file. Adding call-graph expansion (`ctx callers` / `ctx callees`)
from anchors took that instance from 1 of 5 gold files to 2 of 5; the remaining
three are further along edges the policy does not follow. Search and refs
cannot cross a call edge, and no amount of ranking fixes that.

## The defect queue

130 gold files the lane never named at the top budget, listed in
[`contextbench-2026-09-10.json`](contextbench-2026-09-10.json). The queue is
dominated by the language finding above, so the ranked follow-ups are:

1. **Structural engine coverage for JS/TS, C and Go.** These are 17 of 40
   instances and contribute almost nothing. `ctx def` returning nothing is the
   proximate cause on most of them.
2. **A locating verb for prose-only issues** — the probe-starved third. Not a
   ranking change; a different mechanism.
3. **Deeper graph expansion.** Depth-1 `callers`/`callees` from anchors was
   worth real recall on dispersed gold. Depth-2, bounded, is the obvious next
   measurement and `ctx impact` already exists for it.

## Reproducing

```bash
python evals/contextbench.py --workdir /scratch/cb --stratify --limit 40 \
    --budgets 2000,8000,32000 --json evals/contextbench-2026-09-10.json
python evals/contextbench.py --workdir /scratch/cb --language python --limit 12
```

No API key. Network and ~50 MB of scratch per instance (single-commit shallow
fetches, deleted after scoring unless `--keep`). The corpus is cached to
`<workdir>/corpus-verified.jsonl` on first run.
