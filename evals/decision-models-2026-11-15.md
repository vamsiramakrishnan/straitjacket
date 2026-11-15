# Decision models in a coding agent: what we measured

Date: 2026-11-15. Base: `4f03ad29ddc7ea353b2a74089bf948db900d7fca`.
Data tables behind every table below: `evals/decision-models-2026-11-15/`
(small CSV files with aggregates and per-item scores; no transcripts).

## What this page is

We tried a small, fast model in several places inside a coding agent and
measured whether it helped. This page reports every experiment, including the
ones that did not help. Samples are small. Each result has its limits next to it.

Terms used throughout, each defined once:

- **Decision model**: a small, fast model that answers a fixed question about a
  piece of text with yes or no, a choice, or a score. The one used here is a
  hosted decision endpoint. It takes about a third of a second per call and
  costs 0.10 USD per million input tokens (the list price; we did not confirm
  the price tier of the account used).
- **General model**: a general-purpose language model. We used three sizes from
  one vendor, called small, mid-size and large. Each costs more per call than
  the decision model; items 1 and 2 give the costs.
- **Agent**: a general model that edits code and runs commands to solve a task.
  A **session** is one run of an agent on one task. A session is **solved** when
  all of the task's hidden tests pass.
- **Digest**: when a command prints a lot, our tool (`ctx`) gives the agent a
  short summary plus a handle to fetch more. A **follow-up fetch** is a later
  call in which the agent asks for more of that stored output.
- **Benchmark tasks**: DeepSWE tasks, which are feature requests on public
  open-source projects (adaptix, bandit, cattrs, ipython, kombu, mashumaro,
  mobly) with hidden tests.

## Summary

| # | Question | Result |
|---|---|---|
| 1 | Can the decision model do a published style-review task? | Macro F1 0.833, against 0.907 for the published model and 0.919 to 0.930 for the three general models. |
| 2 | Can it find planted bugs? | 31 of 36 found, 5 false alarms in 24. The general models found 34 of 36 with 4 to 13 false alarms. |
| 3 | Does reviewing each edit of 70 recorded sessions catch real failures? | No arrangement named the cause of a failing edit, with one exception. The decision model flagged about 40% of edits. |
| 4 | Can it judge finished work? | No. It scored all four sessions as fully correct, including the two that failed tests. |
| 5 | Does a review at the end, fed back to the agent, help? | It solved 1 of 2 unsolved sessions. A plain "re-check" solved 0 of 2. No decision model involved. |
| 6 | Does listing every distinct error in a long log help, and does a decision model add to it? | The list roughly halved or better the follow-up fetches. The decision model added no measurable difference. |
| 7 | Can it choose which search results to show? | Yes on our proxy measure, with wide uncertainty across projects. |
| 8 | Can it choose which lines of a large file to show? | No clear gain over word matching. |
| 9 | Where does the cost of a coding session go? | Search results are about 1% of cost. The model's own output and its re-reading are about 58%. |
| 10 | Why must selection run before the agent sees the output? | Explanation of prompt caching, not a measurement. |

Rests on the ten experiments below. One row is one experiment; the numbers are
repeated with their sample sizes and limits in each section.

## 1. A published style-review task

The task comes from
[the article "Reviewing every coding agent edit with a 3B decision model"](https://ryanyannelli.com/blog/reviewing-every-coding-agent-edit-with-a-3b-decision-model/).
A fine-tuned 3-billion-parameter model reviews each code snippet against ten
style questions (unclear naming, dead code, too clever, and so on).
**We did not run that model.** We scored the author's own published per-snippet
answers for it on our sample. The other four reviewers were run by us, on the
runs of 13 and 14 November 2026.

Sample: 15 tasks sampled from the article's test split, 4 variants each, so 60
snippets and 600 yes/no decisions. Macro F1 is the average, over the ten
questions, of a score from 0 to 1 that combines how many real flaws were found
with how many alarms were right. The interval is a 95% bootstrap over the 15
tasks.

| Reviewer | Macro F1 (95% interval) | Accuracy | Median time per snippet | Cost per snippet |
|---|---|---|---|---|
| Published 3B model (author's answers) | 0.907 (0.82 to 0.95) | 0.973 | 0.03 s on a data-centre GPU, author's figure | none when run locally, author's figure |
| Decision model | 0.833 (0.74 to 0.90) | 0.958 | 0.34 s | 0.0002 USD |
| Small general model | 0.926 (0.85 to 0.96) | 0.978 | 3.7 s | 0.0011 USD |
| Mid-size general model | 0.930 (0.84 to 0.98) | 0.982 | 2.0 s | 0.016 USD |
| Large general model | 0.919 (0.83 to 0.96) | 0.978 | 2.2 s | 0.033 USD |

Rests on 60 snippets from 15 tasks. One row is one reviewer. Data:
`exp01_style_review_overall.csv`, `exp01_style_review_per_flaw_f1.csv`.

Caveats. The intervals overlap almost completely, so differences of a few
points are noise. The decision model is lowest on three questions: over-abstraction
(0.67), too-clever code (0.67) and type-safety holes (0.73). Every reviewer
judged all 15 clean snippets clean. The published model's speed and cost are
what the author reports for a locally served model; we did not measure them.
Our decision model was asked zero-shot, with the question wording used in the
article; it was not fine-tuned.

## 2. Planted bugs

Twelve small TypeScript modules (25 to 70 lines each, with unit tests). For each
module there are two independent correct versions. Bugs were planted by an
automatic tool that changes one operator, boundary, guard or statement. We kept
only changes that still compile and fail at least one test, and picked 3 per
module with a fixed seed: 36 buggy snippets and 24 correct ones. Each reviewer
answered "is there a bug?". A false alarm is a bug report on a correct snippet.

| Reviewer | Bugs found (of 36) | False alarms (of 24) | False alarms not counting snippets with a confirmed latent defect | Cost per snippet | Median time |
|---|---|---|---|---|---|
| Decision model | 31 | 5 | 4 | 0.0001 USD | 0.33 s |
| Small general model | 34 | 13 | 9 | 0.0009 USD | 3.3 s |
| Mid-size general model | 34 | 4 | 1 | 0.011 USD | 2.3 s |
| Large general model | 34 | 7 | 5 | 0.021 USD | 2.1 s |

Rests on 60 snippets (36 buggy, 24 correct) from 12 modules. One row is one
reviewer. Times are API time for the general models, and about 3 to 4 seconds
more wall time per call. Data: `exp02_planted_bugs_overall.csv`.

What was missed. All 29 bugs of six kinds (arithmetic swap, comparison
swap, off-by-one boundary, negated condition, swapped and/or, changed default)
were found by every reviewer. The misses are all missing code:

| Kind of planted bug | Count | Decision model | Small | Mid-size | Large |
|---|---|---|---|---|---|
| Removed guard clause | 4 | 0 | 2 | 2 | 2 |
| Removed statement | 3 | 2 | 3 | 3 | 3 |

Rests on the 7 planted bugs of these two kinds. One row is one kind.
Data: `exp02_planted_bugs_by_kind.csv`.

Caveats.

- Probes we wrote confirmed that 5 of the 24 "correct" snippets have real defects
  that no test covers (for example, a crash on a special key name, and a year
  range that rejects years below 100). Nine alarms from general models point at
  exactly those lines. The fourth column shows false alarms without them.
  The decision model had one such alarm.
- The modules, their second versions and the three general reviewers all come
  from one vendor. Mechanical bugs are easier than the bugs an agent makes,
  which tend to be missing logic, the one kind every reviewer missed.
- The mid-size and large models named the planted line in 31 and 32 of 34
  catches; the decision model gives no location.

## 3. Reviewing every edit in 70 recorded sessions

We replayed the edits of 70 recorded agent sessions on a small Python shop
library (prices, coupons, tax, dates). 65 sessions passed and 5 failed, with 3
distinct defects among the failures. The agents were a small model (52 passed
sessions) and a mid-size model (13). Every code edit (142 of them; 97 test edits
were left out) was shown, after the fact, to the decision model and to the
mid-size general model. Nothing ran live, so we do not know whether an earlier
flag would have saved or cost turns. Some sessions already had a judge or hint
check running.

| Arrangement | Flags per 100 edits in passed sessions | Passed sessions with at least one flag (of 65) | Defective edits flagged (of 4) | Review cost per session |
|---|---|---|---|---|
| Mid-size general model reviews every edit, shown the edit | 18.3 | 21 | 0 | 0.022 USD |
| Same, also shown the code before the edit | 14.3 | 18 | 0 | 0.025 USD |
| Decision model alone, flags at score 0.5 or more | 39.7 | 38 | 2, with no reason given | 0.0002 USD |
| Decision model filters at 0.5, then mid-size model reviews | 15.9 | 18 | 0 | 0.0098 USD |

Rests on 142 code edits in 70 sessions: 126 edits in 65 passed sessions and 16
edits in 5 failed sessions (4 defective edits among them). One row is one way of arranging the review. Data:
`exp03_replay_arrangements.csv`, `exp03_replay_flag_hand_check.csv`.

What the flags were. We checked all 23 flags of the mid-size model in passed
sessions by hand:

| Flag was | Count |
|---|---|
| Real at that moment, and the agent fixed it in its next edit anyway | 14 |
| A real defect that belongs to another part of the task | 5 |
| Wrong | 4 |
| A new defect that survived to the end and that the tests missed | 0 |

Rests on 23 flags. One row is one kind of flag. For the decision model we
checked a fixed-seed sample of 25 of its 50 flags: none was a surviving
defect, 8 were fixed later, 3 belonged to another part of the task, 14 were wrong.

The failures. The mid-size model did not name the cause of the 4 defective edits
(a thrown error where the test expects no discount, and an email pattern that
accepts a one-letter domain ending); it judged them correct or named a different
problem. The one failure a reviewer explained was an
omission: with the edit alone in view, the mid-size model said that a zero-day
case returns a weekend start unchanged, which is the failing test. With the old
code also in view it called that fault pre-existing, which is right about the
edit and loses the catch. The decision model alone flagged 2 of the 4 defective
edits, but it flagged about 40% of all edits and gave no reason.

Other facts. Four of the 70 sessions could not be rebuilt from edit records,
because the agent changed files with shell commands that an edit check never
sees. The decision model's score was weakly related to which edits the agent
later changed again (area under the curve 0.72, where 0.5 is chance).

## 4. Judging finished work on four benchmark sessions

Four sessions by a small model agent on benchmark tasks: two solved (kombu,
mashumaro) and two not (cattrs lost 1 test, bandit lost 2). We compared review of
each edit with one review of the finished patch.

| Reviewer | Causes of the 3 lost tests named | Items raised in 4 sessions | Cost per session | Share of session cost |
|---|---|---|---|---|
| Mid-size general model on each edit | 0 | 4, all wrong | 0.118 USD | 22% |
| Mid-size general model on the finished patch | 1 | 12, of which 6 wrong | 0.092 USD | 19% |
| Large general model on the finished patch | 2 | 9, of which 1 wrong | 0.251 USD | 51% |
| Decision model scoring the finished patch | 0 | no reasons given | 0.0015 USD | under 0.5% |

Rests on 4 sessions and 3 lost tests. One row is one way of reviewing. Read it
as four case studies, not rates. Data: `exp04_finished_work_arrangements.csv`,
`exp04_finished_work_hand_check.csv`, `exp04_finished_work_sessions.csv`.

| Session | Solved | Code edits an edit check would see | Decision model: probability "fully correct" |
|---|---|---|---|
| cattrs | no | 1 | 0.89 |
| bandit | no | 1 | 0.87 |
| kombu | yes | 19 | 0.93 |
| mashumaro | yes | 17 | 0.87 |

Rests on 4 sessions. One row is one session. Data:
`exp04_finished_work_end_verdicts.csv`.

Caveats.

- In both failed sessions the agent wrote most code through shell commands, so
  a check on file edits would have seen 1 edit each. Two of the three causes
  sit in files no edit tool touched.
- The third lost test (bandit, a text report that closes standard output before
  a later cache report) was named by neither end review. The decision model
  gave no reasons for any of its scores.
- The end reviews also found 3 real defects the hidden tests miss. Their
  verdicts on whether to ship were unreliable: the large model said "ship" for
  bandit (0.55), which failed, and the mid-size model said "no" for three of
  four sessions including two that passed.

## 5. A review at the end, fed back to the agent

No decision model is involved in this experiment. It is context for item 4.

Twelve sessions: 6 benchmark tasks run twice by a small model agent. The agent
solved 10 of 12 alone (mean cost 0.59 USD per session). Two ended unsolved:
bandit-incremental-cache-control (87 of 88 tests) and
cattrs-partial-structuring-recovery (67 of 69). We then gave each session
either the large general model's review of the finished patch, as a list of
defects, or the instruction to re-check its own work. An independent
recomputation of the results confirmed every count below.

| Follow-up | Unsolved starts: solved afterwards | Solved starts: sessions run | Solved starts: tests broken | Solved starts: mean lines changed | Mean cost of the follow-up |
|---|---|---|---|---|---|
| Large model's review, then the agent acts | 1 of 2 | 8 | 0 | 134 | 0.124 USD, plus 0.226 USD for each review |
| Told to re-check its own work | 0 of 2 | 10 | 0 | 65 | 0.105 USD |

Rests on 12 sessions (2 unsolved starts, 10 solved starts). One row is one
kind of follow-up. Data: `exp05_followup_sessions.csv`,
`exp05_followup_changed_code.csv`.

Caveats.

- One rescue in two is not a rate. The rescued session is cattrs.
- Two review follow-ups were skipped because the review raised no items; both
  started from solved sessions. So 8 of 10 solved starts ran after a review.
- Every follow-up that ran changed code, even when the session was already
  solved. Changed lines are measured approximately, as the symmetric difference
  of added and removed lines between patches.
- The cattrs agent edited a test file in every cattrs attempt. The grader resets
  those files before it applies the hidden tests, so the grade is not affected,
  but the gain rests on source edits graded against the reset tests.

## 6. Listing every distinct error in a long log, and a decision model choosing

Two parts, in the same live sessions.

**Part one, no model.** When a command prints a very large log, the digest used
to show the first error, which is often a harmless repeat. The change (merged
as [pull request 56](https://github.com/vamsiramakrishnan/straitjacket/pull/56))
adds an evidence map to the digest: every distinct error message is listed once,
with its count, its first and last line number and a sample, rarest first, and
the digest says when it lists all of them.

**Part two, with a decision model.** On top of the map, the decision model could
choose which messages get a longer excerpt. A third setup replaced it with a
chooser rigged to pick the least relevant messages.

Setup: 10 synthetic logs (build, test, server and similar output), each with one
known decisive line. The agent was a small model, and the task was to say why a
command failed. 20 sessions per setup in each of three batches, run live. The
table reproduces the one in the pull request from the recorded sessions.

| Setup | Batch | Correct | Mean turns | Mean input tokens | Follow-up fetches | Answered with no follow-up |
|---|---|---|---|---|---|---|
| Earlier digest | 1 | 19 of 20 | 5.40 | 212k | 2.80 | 0 of 20 |
| Digest with the evidence map | 1 | 20 of 20 | 3.70 | 152k | 1.05 | 10 of 20 |
| Earlier digest | 2 | 19 of 20 | 4.45 | 177k | 1.90 | 0 of 20 |
| Digest with the evidence map | 2 | 20 of 20 | 3.20 | 120k | 0.40 | 13 of 20 |
| Earlier digest | 3 | 19 of 20 | 4.95 | 189k | 2.40 | 0 of 20 |
| Digest with the evidence map | 3 | 20 of 20 | 4.20 | 163k | 1.10 | 8 of 20 |

Rests on 120 sessions. One row is 20 sessions of one setup in one batch. Data:
`exp06_log_digest_batches.csv`.

With the evidence map, follow-up fetches fell from 1.9 to 2.8 per session to 0.4
to 1.1. Sessions answered straight from the digest went from 0 of 60 to 31 of
60, and correct answers from 57 of 60 to 60 of 60. A session costs about half
a cent in all setups.

| Setup (batches 1 and 2 only) | Batch | Correct | Mean turns | Follow-up fetches | Answered with no follow-up |
|---|---|---|---|---|---|
| Evidence map | 1 | 20 of 20 | 3.70 | 1.05 | 10 of 20 |
| Evidence map and decision model | 1 | 20 of 20 | 3.55 | 0.55 | 13 of 20 |
| Evidence map and rigged chooser | 1 | 20 of 20 | 3.75 | 0.90 | 6 of 20 |
| Evidence map | 2 | 20 of 20 | 3.20 | 0.40 | 13 of 20 |
| Evidence map and decision model | 2 | 20 of 20 | 2.75 | 0.30 | 15 of 20 |
| Evidence map and rigged chooser | 2 | 20 of 20 | 2.75 | 0.45 | 11 of 20 |

Rests on 120 sessions, 20 per row. One row is 20 sessions of one setup. The
decision model made no measurable difference, and the rigged chooser did about
as well as the real one. The pull request gives the reason: the full list of
messages is always printed first, so a bad choice cannot hide one.

How noisy this is. Two further batches of 20 sessions each compared the same
build with itself: 3.10 against 4.35 turns, and 2.95 against 3.30 (follow-up
fetches 0.50 against 1.00 and 0.90 against 0.60). So a difference of one turn
or half a fetch between two groups of 20 is not reliable. The drop in follow-up
fetches is larger than that in every batch. Data:
`exp06_log_digest_noise_check.csv`.

Caveats. The logs are synthetic and the task is of one kind (find why a
command failed). Permission denials differed between setups and move turn counts.
The value of the decision model over the plain rule is not shown by this
experiment.

## 7. Choosing which search results to show

When a search returns more lines than the agent should read, something must pick
which lines to show. We took 147 searches that returned more than 40 lines,
across 7 projects. They were generated by rule from each task's text (every
identifier-like word, plus a few rare words), not typed by an agent. Only 13
real agent searches existed in the four sessions we had; 4 returned more than
40 lines, too few to conclude anything.

A search result is grouped by file. The decision model got the task text and one
yes/no question per file ("Would an engineer doing the task described in the
input need to open this file?", with the path and the first three hits). Files
were shown in order of the model's score.
**Relevant** means the hit lies in a file the reference fix changes. This proxy
undervalues files an engineer would also read (callers, tests), so scores are
lower bounds on usefulness; even the perfect order is only 49% precise.

Chance of showing at least one relevant match within the first 40 lines, with
each project weighted equally:

| Ordering | 40 lines (147 searches) | 20 lines (167 searches) |
|---|---|---|
| The search tool's own order | 0.56 | 0.45 |
| Best simple rule (a path rule with word-match ties at 40 lines; path-word overlap at 20) | 0.66 | 0.46 |
| Decision model, first 300 characters of the task | 0.85 | 0.80 |
| Decision model, whole task text | 0.87 | 0.88 |
| Perfect order | 1.00 | 1.00 |

Rests on 147 searches at 40 lines and 167 at 20 lines, in 7 projects. One row is
one ordering. "Best simple rule" is the best of seven simple orderings (the
tool's own order included), picked on these same searches. Data: `exp07_search_means_40_lines.csv`,
`exp07_search_means_20_lines.csv`.

Per project at 40 lines (decision model with the whole task text):

| Project | Searches | Tool order | Best simple rule | Decision model | Perfect |
|---|---|---|---|---|---|
| adaptix | 8 | 1.00 | 0.50 | 1.00 | 1.00 |
| bandit (taint checks) | 6 | 0.33 | 0.50 | 0.17 | 1.00 |
| cattrs | 14 | 0.57 | 0.57 | 1.00 | 1.00 |
| ipython | 73 | 0.34 | 0.51 | 1.00 | 1.00 |
| kombu | 35 | 0.26 | 0.63 | 1.00 | 1.00 |
| mashumaro | 2 | 1.00 | 1.00 | 1.00 | 1.00 |
| mobly | 9 | 0.44 | 0.89 | 0.89 | 1.00 |

Rests on 147 searches. One row is one project. Data:
`exp07_search_per_project_40_lines.csv`. The model won in 4 projects, tied in 2
and lost in 1. Project sizes range from 2 searches to 73, so the project weighting
rests on few searches. In the one lost project the reference fix adds new files
and edits only a few shared ones, while the model ranked the existing plugins
first. An engineer adding a check might also open those, so this is mostly a
failure of the proxy, on 6 searches.

How large is the gain over the best simple rule? It depends on how the rule is
chosen:

| Weighting | Gain, whole-text model | 95% interval |
|---|---|---|
| Projects equal; rule fixed in advance | +0.21 | -0.05 to +0.43 |
| Projects equal; best rule re-chosen on each resample | +0.21 | -0.05 to +0.37 |
| Every search equal; best rule re-chosen on each resample | +0.25 | +0.18 to +0.33 |
| Every search equal; rule fixed in advance | +0.39 | +0.30 to +0.47 |

Rests on 147 searches in 7 projects. One row is one way to compute the gain at
40 lines. An early draft reported the last row. It is too generous because it
compares against one rule fixed in advance, which was not the best rule in every
resample. Use the middle rows. With projects equal the interval includes zero, so
the gain is not firm. Data: `exp07_search_gain_over_best_rule_40_lines.csv`,
`exp07_search_gain_rule_rechosen.csv`.

At 20 lines the model won in all 7 projects against the single best rule overall.
That holds only for that one rule. With the best rule re-chosen in each resample,
the gain is +0.42 with an interval of +0.14 to +0.52 (projects equal).

Cost: about 0.0007 USD and 0.4 seconds per search. The whole experiment made 354
calls and cost 0.17 USD.

## 8. Choosing which lines of a large file to show

19 files over 240 lines that a reference fix changes, in 8 projects, with a
240-line budget. Each chooser picks functions or blocks to fill the budget. We
measure the share of the reference-changed lines that are shown. Changed lines
include the neighbours of each pure insertion, so a chooser is credited for
showing where new code goes.

| Chooser | Share of changed lines shown | Files with any changed line shown (of 19) |
|---|---|---|
| First 240 lines | 0.42 | 13 |
| Word matching against the task text | 0.50 | 13 |
| Decision model on a list of function names | 0.54 | 13 |
| Decision model on function bodies (600 characters each) | 0.51 | 12 |
| Decision model on function bodies (1200 characters each) | 0.49 | 13 |
| Perfect choice | 0.92 | 18 |

Rests on 19 files in 8 projects. One row is one chooser, with the first 300
characters of the task as the model's input. Data:
`exp08_file_selection_first_300_characters_of_task.csv`,
`exp08_file_selection_per_file_240_lines.csv`.

Against word matching, file by file, the function-list model was better on 6
files, the same on 9 and worse on 4. With the whole task text instead of its
first 300 characters it scored 0.60 against 0.56 for word matching (better on 6,
same on 11, worse on 2). This is no clear gain. Averaging over projects instead
of files gives 0.54 and 0.45 in the first case.

Caveats. One project contributes 5 files and another 4. A difference of 0.05 is
one or two files. On one file, every chooser including the perfect one scores
zero, because the changes sit in a single 566-line function larger than the
budget. Cost: about 0.0005 USD and 0.5 seconds per file for the function list,
and 0.0008 to 0.0009 USD for bodies. Data: `exp08_file_selection_cost.csv`.

## 9. Where the cost of a coding session goes

We rebuilt the cost of the 12 sessions from item 5 call by call. Input that the
provider has cached is billed at a fraction of the normal price. The model assumed
a cache write at 1.25 times, a cached read at 0.1 times and output at 5 times the
normal input price. An independent recheck produced the figures below, and
an earlier analysis of the same sessions agrees on the pattern.

| Part of the session | Share of cost, mean | Range over 12 sessions |
|---|---|---|
| System prompt, tool list and task text | 15.9% | 14.6% to 19.1% |
| The model's own output, and its later re-reading | 57.9% | 49% to 63% |
| One-time cache write of that output | 9.2% | 6.3% to 10.5% |
| File contents shown by cat, sed, head, tail | 6.0% | 2.9% to 10.6% |
| Search results | 1.1% | 0.07% to 3.1% |
| Other command output (tests, builds) | 2.2% | 0.7% to 3.8% |
| Other tool results | 0.8% | 0% to 4.3% |
| Everything else | 6.9% | 5.2% to 11.9% |

Rests on 12 sessions of a small model agent on 6 benchmark tasks (10 solved, 2
not). One row is one part of the bill. Output and re-reading together are 67% when
the one-time write is counted. Data: `exp09_cost_shares_summary.csv`,
`exp09_cost_shares_per_session.csv`.

What perfect selection could save. If every search result kept only a useful
part within 40 lines, and other command output within 100 lines, cost would fall
by about 1% to 2%: 0.95% in the recheck (at most 1.9% in a session) and 1.6% in
the earlier analysis (at most 3.2%). The two analyses count tokens slightly
differently. If long file views were also cut, the saving would be 4.6% (1.6% to
7.1%), but that would remove source the agent chose to read and needs.

Caveats. The price model reproduces the recorded cost of a session only roughly:
a mean error of 9%, and 41% in the worst session. Read each share as good to a
few points. An earlier analysis gave 64% (57% to 72%) for the output component
under a slightly different count. These sessions are one agent model on
benchmark tasks. Of 64 searches the agents made, 29 returned more than 40 lines.

## 10. Why a selection step must run before the agent sees the output

This is an explanation, not a measurement. A provider bills the start of a
conversation, called the cached prefix, at a fraction of the normal price. When
anything early in the conversation changes, everything after it is billed at the
full price again. A step that tidies tool output after the agent has seen it, or
rewrites earlier turns, would break that prefix and could cost more than it
saves. So a selection step has to decide what the agent sees before the output
enters the conversation, and leave the earlier turns untouched.

## What this adds up to

The decision model sorts well and judges poorly.

- It does well at ordering candidates: which files in a search result look
  relevant (item 7, on a proxy measure). It found all 29 planted bugs of the
  operator and boundary kinds, as the general models did (item 2). On the style
  task it was behind the general models (item 1).
- It does badly at judging whether work is correct. It missed missing code
  (item 2), fired on 40% of edits without a reason (item 3), and scored failed
  and solved sessions alike (item 4).
- Where it could pay: workloads where tool output is most of the cost, because
  a selection step can only save what the output it selects from costs. We did
  not measure that share for the log task in item 6. In that task the model-free
  list gave the measured gain, and the decision model on top added nothing
  measurable. So this page does not show a gain from the decision model there.
- Where it cannot pay: coding sessions like the ones measured. Search results are
  about 1% of cost, the agent's own output and its re-reading are about 58%, and
  perfect selection of search and command output saves 1% to 2%.
- A review of finished work by a large general model found a cause in 2 of 3
  lost tests and rescued 1 of 2 unsolved sessions. It costs about as much as a
  fifth to a half of a session. These are four and twelve sessions.

### Limits

- Small samples: 60 snippets, 4 and 12 sessions, 19 files, 147 searches in 7
  projects. Intervals are wide and some cross zero.
- One decision model, one hosted endpoint. Other decision models may differ.
- The logs in item 6 are synthetic and the task is of one kind.
- The searches in item 7 were generated by rule, not typed by an agent. Only 13
  real ones existed.
- Relevance is a proxy: a file the reference fix changes. It undervalues files a
  person would also read.
- One benchmark (DeepSWE) and one family of agent models, mostly one small
  model. The general reviewers and the code in item 2 come from one vendor.
- The recorded sessions in item 3 include some with judge or hint checks, and
  no review ran live.
