# Coding-task suite — a larger, realistic eval set

**Date:** 2026-07-20 · **Harness:** `evals/coding_suite.py` · **Model:**
gemini-3.5-flash (Antigravity SDK) · **Arms:** `naive` vs `sj` ·
**Design:** 5 tasks × 2 arms × 2 repeats = **20 real agent runs** ·
**Record:** `evals/coding-suite-record.json`.

Follow-up to the single-task field test (`field-needle-2026-07-20.md`). That
one probed one flood shape; the pushback was fair — *one* test proves little.
This is a broader set of coding tasks, each a real agent loop against a real
repo fixture, measuring the number that actually bills: **billed_total**
(tokens the provider charged across the whole session, the true cost) and
**tool_output_into_context** (raw tool bytes that entered the transcript, the
mechanism sj acts on). Every run's answer was checked for correctness.

## The tasks

| task | kind | what the agent must do | flood present? |
|------|------|------------------------|----------------|
| `keyword_flood` | flood-keyword | find one needle line in a huge `grep` dump | yes, huge |
| `quiet_flood`   | flood-quiet   | extract a value buried in a large quiet log | yes, huge |
| `traceback`     | traceback     | diagnose a failure from a long pytest dump | yes, medium |
| `bigtest`       | bigtest       | answer from a moderately large file read | small |
| `multifile`     | multifile     | read several small files, synthesize | none |

The set is deliberately mixed: two heavy floods, one medium, and **two tasks
with little or no flood** — because a cost system that only ever helps is one
that was only ever tested where it helps. The last two are where the honest
cost of the wrapper shows up.

## Results (median of 2 repeats; all 20 runs correct)

| task | arm | billed (med) | tool→ctx (med) | calls | wall s |
|------|-----|-------------:|---------------:|------:|-------:|
| keyword_flood | naive | 414,600 | 27,621 | 10 | 28.8 |
| keyword_flood | **sj** | **116,485** | **148** | 8 | 22.2 |
| | | **−71.9%** | 186× less | | |
| quiet_flood | naive | 322,694 | 26,715 | 9 | 24.0 |
| quiet_flood | **sj** | **125,923** | **424** | 9 | 23.9 |
| | | **−61.0%** | 63× less | | |
| traceback | naive | 170,406 | 5,154 | 9 | 22.9 |
| traceback | **sj** | **147,494** | **451** | 9 | 23.2 |
| | | **−13.4%** | 11× less | | |
| bigtest | naive | 105,627 | 266 | 7 | 23.4 |
| bigtest | **sj** | **89,731** | 325 | 7 | 22.9 |
| | | −15.0% (noisy) | — | | |
| multifile | naive | 119,982 | 98 | 7 | 21.1 |
| multifile | **sj** | 114,989 | 163 | 7 | 19.4 |
| | | −4.2% (neutral) | — | | |

**Pooled (10 runs/arm):** naive 170,406 median billed / 5,154 tool→ctx;
sj 116,485 median billed / 325 tool→ctx. Correctness **10/10 both arms**.

## What the numbers say — and where they don't flatter sj

The signal is monotone in flood size, exactly as the mechanism predicts:

- **Heavy flood (keyword, quiet):** −72% and −61% billed. This is the whole
  thesis — 27k tokens of grep/log output never enter the transcript; a bounded
  digest plus a retrieval address goes in instead. The tool→ctx column (186×,
  63× less) is the direct measurement of that substitution.
- **Medium flood (traceback):** −13%. Real but smaller — the pytest dump is a
  few thousand tokens, so bounding it saves a few thousand, not tens of
  thousands.
- **Small read (bigtest):** −15% *but this is noise, not signal.* The two sj
  repeats were 119k and 60k — the spread is larger than the apparent win, and
  gemini-3.5-flash's own turn-to-turn variance dominates at this size. I will
  not claim a 15% saving here; the honest read is "indistinguishable from
  naive, within variance."
- **No flood (multifile):** −4.2%, and sj's **tool→ctx is *higher*** (163 vs
  98). This is the load-bearing honest result: when there is nothing to
  contain, the digest wrapper is pure overhead — a few hundred tokens of
  digest scaffolding on top of output that was already small. sj is at best
  neutral here, and by the mechanism-level metric it costs slightly *more*. A
  system that reports a win on this task would be lying.

## Reading it straight

sj's win is not uniform and it was never going to be: it is a **flood
containment** system, and its benefit is proportional to the flood it
contains. On the two heavy-flood tasks it cuts billed cost by 60–72% with
identical correctness; on the medium one, 13%; on the two low-flood tasks it
ranges from within-noise to a small overhead. That shape — large where output
is large, ~zero (or slightly negative) where output is small — is the correct
and expected behaviour, and it's why the set includes the tasks where sj
*doesn't* help. The mechanism is not a general "make the model cheaper" trick;
it's "don't pay to carry bytes you'll never re-read," and it earns its keep
precisely when there are such bytes.

Reproduce: `python -m evals.coding_suite --arms naive sj --repeats 2`
(requires `GEMINI_API_KEY`; per-run JSON is written under the suite dir and
aggregated into `evals/coding-suite-record.json`).
