# Pathway mining: what the model actually did, from archived transcripts (2026-07-19)

Method: the three spec3 rounds archived complete Claude Code transcripts for
the sj arms (`evals/archive/spec3*-transcripts.tar.gz` — 6 sessions, 2 tasks
× 3 rounds, haiku). A transcript records the model's **original tool inputs**
(PreToolUse steering rewrites the execution, not the recorded intent) and the
digested results it saw, plus the r2/r3 reflex outcome ledgers. That makes
the *naive impulse layer* — what the model wanted to run — observable inside
harnessed history, per call. Analyzer: 172 tool calls, 95 Bash commands
parsed for program, slicers, narrowing, repetition, and retrieval adoption.

Caveat up front: naive and headroom arms were not archived, so their
pathways are inferred from `summary.json` aggregates only. **Recommendation:
spec3_runner should archive all arms' transcripts** — this document is the
proof of what that data is worth.

## The pathway, quantified

| session | calls | Bash | pytest share | full-suite re-runs | Edits | digests seen | ctx verbs used |
|---|---|---|---|---|---|---|---|
| r1/csvq | 32 | 20 | 17/20 | 3× same sig | 5 | 19 | **0** |
| r1/tokenbucket | 32 | 18 | 16/18 | 3× same sig | 10 | 16 | **0** |
| r2/csvq | 20 | 14 | 9/14 | — | 2 | 10 | **0** |
| r2/tokenbucket | 24 | 15 | 11/15 | 3× same sig | 5 | 12 | **0** |
| r3/csvq | 32 | 10 | 8/10 | — | 13 | 8 | **0** |
| r3/tokenbucket | 32 | 18 | 13/18 | — | 8 | 14 | **0** |

Aggregate: **67 of 95 Bash calls are pytest** (70%). The spec-driven session
*is* a test-edit loop; everything else (mkdir, ls, heredoc data setup) is
prologue. 44 of 67 pytest runs were full-suite; only 23 narrowed with `::`.

## Findings

**F1 — The model is its own ad-hoc rtk.** Naive flood-control impulses,
counted: `-v` on 47 pytest calls, `2>&1` on 32, `--tb=` flags 16, `| tail`
13, `| head` 10, `-x` fail-fast 10, `| cat -n`/`| grep` 14. Note the
contradiction: the model *asks* for verbosity (`-v`, `--tb=long`) and then
*trims* it lexically (`| tail -200`). Under a naive arm those habits are the
flood; under the harness the slicer-normalized signature machinery
(ctx.reflex) was built for exactly this — and these counts are its empirical
justification. The bar it sets: a digest must beat the model's own
`| tail` at answering the model's next question, or the model routes
around containment with pipes.

**F2 — Retrieval adoption was zero. Re-running is the model's page fault.**
Across 6 sessions, 79 digested results, and 15 ledgered starvation events:
not one `ctx get` or `ctx search` call. When a digest under-fed it, the
model re-ran the whole suite (2 turns, fresh cache-write) rather than
resolving an address it was explicitly handed in `next:` lines. This
generalizes the ctx-eval adoption debt: **the model acts in the command
channel, not the verb channel.** The mechanism implication is sharp — put
continuations *into the command channel*: a failing-census digest should
suggest `pytest tests/test_csvq.py::TestOperators -x` (a narrowed re-run the
model will actually issue, which the harness then digests) at least as
prominently as `ctx get`. Meeting the model where it acts beats teaching it
a new place to act. (The 0.21 failing census + 0.22 EDC already move this
way; the suggestion-shape change is the residue.)

**F3 — The 33-turn-cap loss, seen from inside.** r1 sessions burned turns in
`pytest -v` → Edit → `pytest -v` cycles where the pre-census digest named
one failure at a time — serial discovery of a parallel work queue. The
enumeration regime again (rtk-corpus lint-fix lesson): when the flood IS the
work queue, census-first structure is the only fix. Visible improvement
across rounds as those mechanisms landed: r3/csvq collapsed to 10 Bash calls
and 13 surgical Edits with only 8 digests — the loop inverted from
probe-heavy to edit-heavy.

**F4 — The ctx-eval denominator is real.** 8 `python -c`/heredoc one-liners
appear across sessions (inspect-signature probes, test-data setup) — exactly
the programmable-capture opportunities the v0.20 teaching surface detects.
Zero used `ctx eval`. The adoption ratio's denominator is confirmed nonzero
in real sessions; conversion remains the metric to move.

**F5 — What naive did right.** summary.json: naive won r1 csvq outright —
11 turns / $0.082 vs sj's 33 / $0.308 at identical 7/7 holdout. Its edge was
structural, not skill: full pytest output inline is the entire work queue in
one look, and in a *small* task the flood never grows large enough to
punish. The harness's worst regime is a task where nothing needs containing
— which is why graduated engagement exists, and why the win condition there
is parity, not victory. What naive does wrong at scale is already receipted
elsewhere (needle-drop, overhaul, matrix evals); nothing in these
transcripts contradicts it.

## Coverage implications (ties to evals/coverage-corpus-2026-07-19.md)

The observed command distribution ranks digest investment: test runners
dwarf everything (70% of commands — pytest census, cargotest/v1 validated as
the right priority), interpreter one-liners are the second family (F4), and
the tables/CI-logs families don't appear *in creation tasks* — their case
rests on ops/babysitting workloads, so the next transcript corpus to mine
should be a PR-babysit or debugging session, not another spec build.

## Actions this report earns

1. Archive naive/headroom arm transcripts in spec3_runner (small change,
   permanent observability).
2. Command-channel continuations: failing-census digests suggest narrowed
   re-run commands, not only ctx verbs (F2).
3. Track verb adoption per session in the scorecard (verbs used / digests
   seen / starvations) — the number that was silently 0/79/15 here should
   never be invisible again.
