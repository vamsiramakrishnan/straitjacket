# Anchor drift — how often a `repo:` address stops meaning what it meant

**Date:** 2026-08-20 · **Revalidated:** 2026-09-02 · **Referee:**
`evals/anchor_drift.py` (model-free, corpus commit `7c69ea70aa40`, seed
20260820) · **Record:**
[`anchor-drift-2026-08-20.json`](anchor-drift-2026-08-20.json)

Reproduce:

```bash
python evals/anchor_drift.py          # the table below
python evals/anchor_drift.py --json   # the record
```

## The question

For a retained immutable artifact, a handle addresses the same stored bytes.
A `repo:` address is different: it points into a file the agent is concurrently
editing, where a line number is a position rather than an identity. This
receipt measures that mutable case.

This instrument does not ask *does anchoring work*; the acceptance tests pin
that. It asks **how large the exposure was**, and what closing it costs.

## Method

Corpus: 40 of this repository's own `src/ctx/*.py` files, ≥120 lines each, at
commit `7c69ea70aa40e1017aa6114b19e977225dd4166f` — real indentation, duplicate
lines, and docstrings, which is what makes relocation a measurement rather than
a demonstration. Pinning the corpus prevents unrelated source edits from
changing the sampled spans. The resolver under test still comes from the
current tree, and CI requires its output to equal the committed JSON.

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
insert-above          480             478         2        478        0             0
delete-above          480             480         0        480        0             0
move-the-span         480             480         0        480        0             0
rewrite-the-span      480             480         0         14      466             0
ALL                  1920            1918         2       1452      466             0

unanchored: 99.9% of re-resolutions returned different content, exit 0, no note
anchored:   75.7% answered correctly (1452 of them by following content that moved), 24.3% refused, 0 wrong answers
cost:       +17280 characters over 84028 of address (20.6%)
```

## Reading it

**The unanchored column is an exposure, not a field base rate.** Every case here
edits above or into the span, so 99.9%
silent drift is what happens *given* a shifting edit — not the fraction of all
reads that go wrong in practice. What it measures is that when an unanchored
`repo:` address does go stale, it goes stale **quietly and completely**: exit 0,
a plausible-looking body, no note. There is no partial failure mode to notice.

**Relocation, not detection, is where the value is.** Of
1454 correct answers, 1452 came from
following content that had moved. A mechanism that only *detected* staleness
would have been right 2 times and refused
99.9% of the time — technically
honest and practically useless. Answering correctly across a shift is what makes
an anchor a working identifier.

**The refusals are the irreducible ones.**
466 of 466 refusals are
`rewrite-the-span`, where the addressed content no longer exists anywhere. No
addressing scheme can resolve those; the available win is converting a wrong
answer into a refusal, and that is what happened. The remaining
14 `rewrite-the-span` cases
*relocated* — the span's content was byte-identical to another region of the
same file (blank lines, repeated boilerplate). Those returned the addressed
bytes, so they are correct under the promise an anchor makes, but they are the
known limit: an anchor recovers **content**, not **identity**.

**Zero wrong answers.** Across 1920 resolutions, no anchored read returned content
other than what its address named.

**Cost:** +17280 characters across
84028 of address text — **20.6%**
on the address, and a rounding error against the span being addressed.

## Negative findings and limits

- **`insert-above` verified twice.** Two sampled spans contained repeated text
  that also occupied the original coordinates after insertion. Correct under
  the content promise, and a reminder that the shapes here are a sample of edit
  behaviour, not a proof over all edits.
- **This receipt measures the mechanism, not agent behaviour.** It shows that an
  anchored address survives edits that break a bare one. It does **not** show how
  often a real agent replays a stale address, which needs session telemetry — the
  same distinction `evals/command_corpus.py` draws between a surface's coverage
  and its use. Until that exists, the decision to leave `ctx refs` and `ctx diag`
  rows unanchored is a cost judgement, not a measured one.
- **Model-free by design.** No LLM is involved, so nothing here can drift with a
  vendor's weights. The corpus is commit-pinned, the current resolver can still
  change the result, and the JSON equality check makes that behavior drift
  visible. The referee re-runs in a review sandbox in seconds.
