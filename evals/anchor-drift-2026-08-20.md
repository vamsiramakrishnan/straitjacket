# Anchor drift — how often a `repo:` address stops meaning what it meant

**Date:** 2026-08-20 · **Referee:** `evals/anchor_drift.py` (model-free,
deterministic, seed 20260820) · **Record:** [`anchor-drift-2026-08-20.json`](anchor-drift-2026-08-20.json)

Reproduce:

```bash
python evals/anchor_drift.py          # the table below
python evals/anchor_drift.py --json   # the record
```

## The question

straitjacket's second house rule is *omission keeps an address*, and the README
states the consequence: **"the same address returns the same bytes tomorrow."**
That is enforced and measured for the immutable side of the store. It was never
measured for `repo:` addresses — line numbers into a file the agent is
concurrently editing — and it is not true there.

This instrument does not ask *does anchoring work*; the acceptance tests pin
that. It asks **how large the exposure was**, and what closing it costs.

## Method

Corpus: 40 of this repository's own `src/ctx/*.py` files, ≥120 lines
each — real indentation, real duplicate lines, real docstrings, which is what
makes relocation a measurement rather than a demonstration.

For each file, spans of 2, 8, 25 lines
are sampled at random positions. Each span's address is minted **before** an
edit and re-resolved **after** it, under four edit shapes:

| shape | what it does |
|---|---|
| `insert-above` | three lines added above the span — the commonest shift there is |
| `delete-above` | three lines removed above the span |
| `move-the-span` | the span itself relocated, content intact — the case a whole-worktree generation guard cannot distinguish from a deletion |
| `rewrite-the-span` | the span's own content replaced — unrecoverable by construction |

Both resolutions run against the same post-edit file: the unanchored one returns
whatever now occupies those coordinates (which is what `ctx get --lines A:B`
does), the anchored one runs the shipped verify → relocate → refuse ladder.

Every answering resolution is additionally checked against the content the
address originally named. **A single wrong answer would be a defect, not a
statistic.**

## Result

```text
[anchor drift · 40 files · 1920 resolutions · seed 20260820]

Every case: an address minted before an edit, re-resolved after it.

edit shape          cases  silently wrong  verified  relocated  refused  wrong answer
insert-above          480             480         0        480        0             0
delete-above          480             479         1        479        0             0
move-the-span         480             480         0        480        0             0
rewrite-the-span      480             480         0         15      465             0
ALL                  1920            1919         1       1454      465             0

unanchored: 99.9% of re-resolutions returned different content, exit 0, no note
anchored:   75.8% answered correctly (1454 of them by following content that moved), 24.2% refused, 0 wrong answers
cost:       +17280 characters over 83788 of address (20.6%)
```

## Reading it

**The unanchored column is an exposure, not a field base rate.** Every case here
edits above or into the span, so 99.9%
silent drift is what happens *given* a shifting edit — not the fraction of all
reads that go wrong in practice. What it measures is that when an unanchored
`repo:` address does go stale, it goes stale **quietly and completely**: exit 0,
a plausible-looking body, no note. There is no partial failure mode to notice.

**Relocation, not detection, is where the value is.** Of
1455 correct answers, 1454 came from
following content that had moved. A mechanism that only *detected* staleness
would have been right 1 time and refused
99.9% of the time — technically
honest and practically useless. Answering correctly across a shift is what makes
an anchor a working identifier.

**The refusals are the irreducible ones.**
465 of 465 refusals are
`rewrite-the-span`, where the addressed content no longer exists anywhere. No
addressing scheme can resolve those; the available win is converting a wrong
answer into a refusal, and that is what happened. The remaining
15 `rewrite-the-span` cases
*relocated* — the span's content was byte-identical to another region of the
same file (blank lines, repeated boilerplate). Those returned the addressed
bytes, so they are correct under the promise an anchor makes, but they are the
known limit: an anchor recovers **content**, not **identity**.

**Zero wrong answers.** Across 1920 resolutions, no anchored read returned content
other than what its address named.

**Cost:** +17280 characters across
83788 of address text — **20.6%**
on the address, and a rounding error against the span being addressed.

## Negative findings and limits

- **`delete-above` verified once.** One sampled span sat at a position where
  deleting three lines above it happened to leave the span's content at the same
  coordinates. Correct, and a reminder that the shapes here are a sample of edit
  behaviour, not a proof over all edits.
- **This receipt measures the mechanism, not agent behaviour.** It shows that an
  anchored address survives edits that break a bare one. It does **not** show how
  often a real agent replays a stale address, which needs session telemetry — the
  same distinction `evals/command_corpus.py` draws between a surface's coverage
  and its use. Until that exists, the decision to leave `ctx refs` and `ctx diag`
  rows unanchored is a cost judgement, not a measured one.
- **Model-free by design.** No LLM is involved, so nothing here can drift with a
  vendor's weights, and it re-runs in a review sandbox in seconds.
