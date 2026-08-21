# Edit repair — the ceiling, before deciding whether to build it

**Date:** 2026-08-20 · **Referee:** `evals/edit_repair.py` (model-free,
deterministic, seed 20260820) · **Record:** [`edit-repair-2026-08-20.json`](edit-repair-2026-08-20.json)

```bash
python evals/edit_repair.py          # the table below
python evals/edit_repair.py --json   # the record
```

## Why this exists

straitjacket's PreToolUse hook sees every `Edit`/`Write`/`MultiEdit` the host
issues, and on Claude Code and Codex it can **rewrite those arguments** before
the tool runs. That makes repairing a failed edit — the field's most-cited
harness failure, where the model could not reproduce a region byte-for-byte —
something this project could plausibly ship.

"Could plausibly ship" is not a reason to ship. Two questions come first:

1. **How much of a failed edit is recoverable at all**, and how much can only
   be refused? That is a ceiling, and this instrument measures it.
2. **How often does each failure shape actually occur?** That is field data.
   This instrument cannot know it and does not guess;
   [`ctx.edit_outcomes`](../src/ctx/edit_outcomes.py) now records it.

Both numbers are needed. This receipt supplies only the first, and the
distinction is the most important thing on this page.

## Method

40 of this repository's own `src/ctx/*.py` files, ≥120 lines each.
Regions of 2, 6, 15 lines are sampled
at seeded random positions. For each region, the eval constructs the
`old_string` a model would emit for it under one of six shapes, then asks what
the host's exact-substring edit does with it, and what a content-based repair
could do afterwards.

The repair modelled here is deliberately narrow: it may treat **whitespace and
blank lines** as insignificant, and nothing else. Identifiers, punctuation,
string contents and line order are all load-bearing. It resolves only when
**exactly one** region matches; two or more is a refusal, never a choice.

## Result

```text
[edit repair ceiling · 40 files · 3721 simulated edits · seed 20260820]

Each case: a real region, the old_string a model would emit for it,
and what the host's exact-substring edit does with that today.

how the model got it wrong  cases  applies  notfound  multi  →repair  →ambig  →gone  wrong
verbatim                      710      693         0     17        0       0      0      0
reindent                      667       20       641      6      497     144      0      0
respace                       668       18       644      6      500     144      0      0
tabs-for-spaces               667        2       665      0      514     151      0      0
blank-lines-dropped           305       58       247      0      165      26     56      0
word-changed                  704        0       704      0        0       0    704      0

Read the arms separately. There is no blended success rate here: how often each
shape actually occurs is field data (ctx.edit_outcomes records it), not something
this file can know, and averaging the arms would just report how many of each
kind happen to be written above.

the model reproduced the right region imperfectly (2197 failures)
  repair resolves       1676  (76.3%)
  refuses as ambiguous   465  (21.2%)
  cannot find             56  (2.5%)

the model named different content (704 failures) — the control
  repair resolves          0  (must be 0)

exact-substring matching already failed 29 times on a PERFECT reproduction,
because the region occurs more than once. Repair cannot help there and must not try:
several equally good matches is the model's ambiguity, not a lookup problem.

repairs that landed on the wrong region: 0 of 1676
```

## Reading it

**Whitespace-shaped failures are 76% recoverable.** Of
2197 failures where the model had the right region but reproduced its
indentation or spacing differently, repair resolves 1676 unambiguously and
refuses 465 because more than one region matched. This is the case worth
building for, and it is the majority of what reproduction error looks like.

**Content-shaped failures are 0% recoverable, by construction.** The
`word-changed` arm is the control: one identifier in the region is altered, so
the model is no longer naming the region it read. All
704 fail, and repair resolves **none** of them. A
mechanism that "helpfully" recovered these would be choosing an edit target the
model never asked for, which is worse than the failure it replaces.

**Repair never landed on the wrong region: 0 of 1676.** This is the
property the whole idea rests on. One wrong resolution would mean silently
writing a change into code the model was not looking at, and no recovery rate
would justify it.

**29 edits failed on a *perfect* reproduction**, because the
region occurs more than once in the file. Repair cannot help here and must not
try: several equally good matches is the model's own ambiguity. This is a
different mechanism — an anchored region address
([ANCHORS](../docs/ANCHORS.md)) disambiguates by position where content cannot.

## What this does NOT establish

- **No field rate.** Every arm gets equal weight here because the eval has no
  basis for any other weighting. A blended "repair fixes X% of edits" number
  would be reporting how many arms of each kind this file happens to define.
  The per-arm rates are the result; the mixture is unknown until
  `ctx.edit_outcomes` has collected real sessions.
- **No claim about agents.** These are simulated reproduction errors, chosen
  because they are the shapes hosts report. Whether real models fail this way,
  and in what proportion, is exactly what the field instrument is for.
- **Antigravity gets nothing from this.** Its published hook contract has no
  input substitution and no PostToolUse payload, so it can neither be measured
  by the instrument nor repaired by the mechanism. Given the project is built
  for Antigravity first, that is a material limit on the whole direction and
  belongs in the build decision rather than a footnote to it.

## The build decision this supports

Repair is worth building **if** the field ledger shows `not_found` is a
meaningful share of real edits. The ceiling is good (76%
of reproduction error, zero wrong answers) and the refusal behaviour is sound.
What is missing is the rate — so the instrument ships first, the mechanism
waits on its numbers, and this receipt is the thing that gets re-read when they
arrive.
