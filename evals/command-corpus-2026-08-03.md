# Command corpus: what agents actually run

**Instrument:** [`evals/command_corpus.py`](command_corpus.py) — a read-only
scan over stream-json transcripts. Coverage is computed by calling the real
`ctx.substitute.collapse`, not a re-implementation of its rules, so the number
cannot drift from the shipped behaviour.

**Corpus:** 5,363 shell commands from 19 recorded agent sessions (the
bug-bash A/B arms, harnessed and naive, rounds 3–16), 71 distinct programs.
Same repository throughout — see *Limits* before generalising any of this.

**Why it exists:** the replacement surface went from three shapes to eight, and
all eight were chosen **by inspection**. That is the same guessing this project
criticised in others. This is the instrument that replaces it — and its first
act was to overturn a claim I had just made.

---

## 1 · The correction, first

Before running this I sampled two uncovered `grep` commands, saw both were
globbed multi-file searches, and concluded that ~466 uncovered greps (8.7% of
all commands) were one rung away from coverage.

The corpus says otherwise:

| Uncovered `grep` commands | n | share |
|---|---|---|
| **single concrete file** — already bounded, correctly declined | 410 | **88.4%** |
| multiple explicit paths — not expressible as one `--glob` | 52 | 11.2% |
| no target | 2 | 0.4% |

The rung was already correctly scoped. My estimate was off by roughly an order
of magnitude because it was drawn from two samples. Two samples is not a
distribution, and this is precisely the failure mode the instrument exists to
prevent — including when the person guessing is the one building the thing.

## 2 · The structural ceiling: 64% of commands are compound

| Verdict | n | share |
|---|---|---|
| compound (pipe / redirect / chain / `$(…)`) | 3,449 | **64.3%** |
| bare, no rung | 1,619 | 30.2% |
| substituted today | 280 | 5.2% |
| unparseable | 15 | 0.3% |

**Roughly two-thirds of everything an agent runs is a composed command**, and
the replacement surface declines those categorically — rewriting one half of
`grep … | head -20` changes what the whole thing means. That is a deliberate
rule, not a gap, and it is the ceiling on this entire approach: a
bare-invocation surface can never address more than ~35% of commands.

This reframes the rtk comparison. Their 100+ intercepted commands and our 8
are not the same axis: what bounds reach here is the compound rule, not the
rung count. Adding twenty more rungs would move the ceiling by very little.
The interesting question is the one this measurement raises — whether a
*pipeline-aware* recogniser (rewriting `grep … | head -20` to a bounded
equivalent, as a unit) is worth building. That is a substantially harder
recogniser, and it is where the remaining headroom actually lives.

## 3 · The distribution

| Program | n | share | |
|---|---|---|---|
| `grep` | 1,441 | 26.9% | 893 compound, 410 single-file (correctly declined) |
| `ctx` | 904 | 16.9% | already ours |
| `cd` | 782 | 14.6% | not a read |
| `python3` | 637 | 11.9% | mostly repro scripts — `ctx py` territory, not substitution |
| `sed` | 190 | 3.5% | rung added |
| `export` | 153 | 2.9% | not a read |
| `wc` | 148 | 2.8% | rung added (single source file) |
| `find` | 130 | 2.4% | rung added |
| `cat` | 123 | 2.3% | pre-existing rung |
| `ls` | 101 | 1.9% | rung added (`-R` only) |
| `git log` / `git show` | 115 | 2.1% | **no rung** — see below |

Once `ctx` itself, the navigation verbs (`cd`, `export`, `mkdir`, `rm`) and
correctly-declined single-file reads are removed, the genuinely addressable
uncovered surface is small. The eight rungs are, by this measure, roughly the
right eight — arrived at by luck as much as judgement.

## 4 · What this changed

**A real widening bug, found while acting on the data.** `_scope_hint` ran a
`\*\.\w+$` branch before its general glob branch, so `src/ctx/*.py` returned
`*.py` — *the whole repository instead of one directory*. For a bare `*.py`
the two are identical, which is why it looked right; it only widened once a
directory prefix was present. That is the exact defect the function's own
docstring says it exists to prevent, hiding inside the branch meant to
preserve the caller's glob. Fixed by ordering the general case first.

**Non-recursive grep over many files now collapses.** The `-r` flag was the
wrong discriminator: what matters is whether the target names one file or
many. A glob character means many; so does a bare directory. Single concrete
files still decline — they are already bounded.

Coverage moved 5.0% → 5.2%. That is a small number and it is the honest one:
the corpus says most of what looked like headroom was already being declined
for good reasons.

## 5 · Limits

- **One repository.** Every session ran against straitjacket itself, a Python
  project with an unusual density of `ctx` invocations (16.9% of all commands
  are our own tool). The `grep`-heaviness and the near-absence of `npm`,
  `cargo`, `docker` and `make` are artefacts of that, not findings about
  agents.
- **One agent family.** All arms were Claude Code sessions.
- **Task-shaped.** These were bug-hunting sessions. A refactoring or
  greenfield corpus would look different — likely more build/test commands and
  fewer searches.

None of these limits affect the two findings that matter most (the 64%
compound ceiling and the `_scope_hint` bug), both of which are structural
rather than distributional. They do bound the *distribution* table above, and
it should not be quoted as "what agents run" without this paragraph attached.

## 6 · Next

- **Pipeline-aware recognition** is where the remaining headroom is (64% of
  commands). Hard, and worth scoping before building.
- **`git log` / `git show`** (2.1%, no rung) is the largest genuinely
  uncovered read family. Both have bounded `ctx run` capture today; whether a
  dedicated rung beats that is untested.
- **A second corpus from a different repository and language** would tell us
  which of the numbers above are about agents and which are about us.

Reproduce:

```bash
python evals/command_corpus.py <dir-containing-stream.jsonl-files>
python evals/command_corpus.py --json <dir>    # machine-readable
```
