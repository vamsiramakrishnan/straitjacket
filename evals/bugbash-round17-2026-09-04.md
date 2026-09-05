# Bug-bash round 17 — S6 cell, naive vs harnessed Claude Code (2026-09-04)

**Date:** 2026-09-04 · **Cell:** S6 bug hunt (`evals/matrix_runner.py`, task
text unchanged since 2026-07-18) · **Model:** host default (`claude -p`,
30-turn cap) · **Arms:** naive (bare `claude -p`) and harnessed
(`ctx wrap claude --proxy`), each in a fresh clone of the branch under test
with an isolated `CLAUDE_CONFIG_DIR` · **Harvest:** `tests/test_round17_mechanisms.py`

```bash
python evals/matrix_runner.py --repo . --out /tmp/bb17 --pairs S6:sonnet
```

## Layer 1 — arm metrics

| arm | findings delivered | turns | wall | cost | subagents | note |
|---|---|---|---|---|---|---|
| naive | 9 ranked | 7 | 98 s | $13.80 | 6 spawned, 6 completed | main agent waited for its subagents, then verified each candidate itself |
| harnessed (sj) | **0** | 17 | 155 s | $22.72 | 7 spawned, **7 killed** | main agent ended its turn while the subagents ran; print mode's 600 s background ceiling killed them before any reported |

Harnessed-arm proxy scorecard (the naive arm has no proxy and no scorecard):
934 rounds across the 7 subagents · 300,561 output tokens · 99.0 % cache
hit · 388 invalidations · cold prefix 35,463 tokens.

**What changed since the July S6 round, and why the turn counts do not
compare to that table.** This container's Claude Code exposes the subagent
tool to both arms, so both fanned the tree out across 6–7 parallel hunters
instead of walking it single-threaded. That is the same condition on both
sides and keeps the pair fair, but it moved the decisive variable from
"how the tree was read" to "did the main agent keep its turn alive until
the subagents reported". The naive arm did; the harnessed arm scheduled a
wakeup, ran one no-op command, and ended its turn — at which point print
mode terminated its background work. Nothing in the ctx skill or hooks
asks for that behaviour; it is one sample of a stochastic choice, and the
$22.72 bought seven partial transcripts rather than a report. Those
transcripts were mined by hand (below). A second harnessed run was not
bought: the deliverable of this round is verified defects, and the naive
report already carried nine.

## Layer 2 — finding verification (the real deliverable)

**Naive arm: 9 ranked by the main agent plus 1 more in a subagent report it dropped → 10 reproduced → 10 fixed.** Every claim was
re-verified against the tree before its fix landed: four by executing the
failing scenario directly (`classify_command`, `load_config`, `_epoch_rung`,
`_rank`), five by reading the site and writing the failing test first. All
16 defect tests in `tests/test_round17_mechanisms.py` fail on the tree as it
was and pass after the fix; the other two tests in that file pin behaviour
that must NOT change (the redirect shortcut still answers the volume
question; a list-valued `lean_models` still loads).

| # | Defect | Site | Severity | Verified |
|---|---|---|---|---|
| 1 | `cmd > file 2>&1` shortcut returned `allow` ahead of the secret-path guard: `cat .env > out.log 2>&1` was allowed while `cat .env` force-asks | `hook.py` `_classify_command_inner` | high (security) | reproduced |
| 2 | `[engagement] lean_models = 42` raised `TypeError` out of `load_config`, on every command's path; `"sonnet"` became six one-letter models | `config.py` | high | reproduced |
| 3 | `digest_output` hard-coded `exitCode: 0`; an errored over-budget tool result digested as `exit 0` and its stored manifest remembered a success | `digest/__init__.py` | high | reproduced |
| 4 | `_epoch_rung` indexed `rungs[2]` unconditionally (`[x] * 0` still evaluates `x`); a two-rung `[ladders.epochs]` crashed `ctx ladders` | `ladders.py` | medium | reproduced |
| 5 | builtin ranker raised `KeyError` on an import edge to a listed-but-unreadable file; the networkx ranker already guarded it | `repomap.py` `_rank` | medium | reproduced |
| 6 | gateway `_rpc` bounded only the first byte: `select` then `readline()` blocked without a deadline on a backend that wrote half a line and hung | `surface_gateway.py` | medium | reproduced (thread-guarded test) |
| 7 | `failing_ids` filter tested the whole result, never the match: one FAILED in a `pytest -v` run tagged every passing id as failing | `evidence_outcomes.py` | medium | reproduced |
| 8 | `if u_read and u_read < max_read` skipped the largest invalidation, cache_read collapsing to 0 | `scorecard.py` | medium | reproduced |
| 9 | `follow_symlinks = true` was a no-op on the ripgrep engine (`--follow` never passed) | `_retrieval/rg_engine.py` | low | reproduced |
| 9c | parallel wave nodes raced on one git worktree id: every checkout was `<tmp>/repo`, so two `git worktree add` calls contended for `.git/worktrees/repo` and one read a half-written entry | `worktree_isolation.py` | medium | reproduced by CI on this branch (1 of 2 identical runs), not found by either arm; fixed with unique leaf names and a process lock |
| 9b | `fails_sites` served a gc-collected run's stale census from the cached `latest_run` pointer, with a `run:` citation that no longer resolved | `facts.py` | medium | reproduced (naive subagent finding the main agent dropped from its ranked nine; kept because it held) |

**Harnessed arm, from the killed transcripts: 3 candidates → 1 duplicate,
2 refuted.** One subagent had independently found #2 (`lean_models`, "the
`_str_tuple` usage difference") and was raising its confidence when it was
killed. One reported "read-modify-write with no locking" on the native-read
ledger charge — refuted: `_ledger_charge` holds `fcntl.flock` across the
read and write, the same idiom the taskledger fix adopted this week. One
suspected a `shown = 1` overwrite in the lint profile — refuted: that
assignment is on the single-diagnostic branch, where one is the count.

## Reading it

1. **The tree still has real defects, and two are old lessons through new
   doors.** The redirect shortcut had been fixed once already (S6, July) to
   stop returning ahead of the repo's deny list; it still returned ahead of
   the secret guard. `lean_models` was the one list field left iterating
   raw TOML after `deny_commands = 42` taught this file to coerce (round
   15). Both fixes now route the second door through the same predicate as
   the first (`_names_secret_path`, `_str_tuple`), which is the only shape
   of fix that closes the class.
2. **Harness value did not get measured this round.** The harnessed arm's
   zero is a print-mode lifecycle outcome, not a containment outcome, and
   a single sample. The scorecard shows what the harness *did* observe
   (99 % cache hit over 934 subagent rounds); it cannot say what the
   harnessed report would have contained.
3. **Precision held at 1.0 for the report that arrived.** Ten of ten
   claims reproduced; the two refutations came from the arm that never
   finished verifying, which is the step the naive main agent spent its
   last turns on.
4. **Root cause, established from the transcripts (not speculation).**
   `harnessed-arm.main-transcript.jsonl` ends with the main agent's own
   text — "Bug-hunt agents are running in the background across all of
   src/ctx; I'll compile and verify the findings once they report back" —
   preceded by a `ScheduleWakeup` call whose tool result states "Next
   wakeup scheduled … Nothing more to do this turn — the harness
   re-invokes you when the wakeup fires or a task-notification arrives,"
   and a no-op `Bash("true")` call with the comment "waiting for agent
   notifications." That belief is false for a `claude -p` process: there
   is no persistent harness to re-invoke it, only the CLI's own print-mode
   background-agent supervisor, which (per `harnessed-arm.stderr.txt`)
   waited `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (600 s, unset) after the
   turn ended and then killed all 7 subagents (`result.json`:
   `"killed": {"system": 7}`, `"completed": 0`) before any could report —
   discarding real in-progress work
   (`evidence/harnessed-arm.subagent-notes.txt`). We checked every
   ctx-injected surface for a push toward that decision: the persistent
   CLAUDE.md block (`installer.py::_render_claude_md`) was not even
   installed for this ephemeral `ctx wrap claude` run; the PreToolUse/
   PostToolUse hook output visible in the transcript only routes Bash
   through `ctx run` and nudges terser narration
   (`CTX_EMISSION_GOVERNOR`); and `wrap.py`'s own injected system-prompt
   nudge says the opposite of what happened — "prefer acting over
   describing what you will do." The hooks around the final no-op command
   took 33 and 37 ms and returned plain allow. The wakeup tool the agent
   believed was present in BOTH children only because the launch inherited
   the parent remote session's environment (its session id and tool set);
   the naive arm called it once too, then kept polling for its subagents'
   notifications until all six had reported, while the harnessed main
   agent ended its turn on the tool's reply. One sample each: the split is
   a stochastic choice under an inherited tool, not a harness effect, and
   the per-tool-call latency story the first draft of this section told is
   not supported by the transcript. **Fix:** `ctx wrap claude` now defaults
   `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` (wait indefinitely) whenever it
   launches Claude Code in print mode, `ctx orchestrate`'s launcher sets
   the same default for its `claude -p` nodes (bounded by its own per-node
   timeout), and `evals/matrix_runner.py` sets it for both arms of a pair
   so this ceiling is never the thing a naive-vs-harnessed comparison is
   actually measuring. None overrides a value already present in the
   caller's environment. The runner also no longer inherits the parent
   session's identity: it drops the `CLAUDE_CODE_SESSION_ID` and
   `CLAUDE_CODE_CHILD_SESSION` variables so each arm is its own session.

**Defence (2026-09-05).** The timer fix above stops print mode from killing
the subagents; it does nothing about the belief that made the main agent
end its turn to wait for them. `ScheduleWakeup` is built into this Claude
Code build (2.1.261) and cannot be stripped from a print-mode child by
environment — probing with every `CLAUDE_CODE_*` variable removed still
left the tool present — and its tool result still says "the harness
re-invokes you," which is only true in an interactive session. `ctx wrap
claude` (`src/ctx/wrap.py::_with_output_discipline`, new
`_SINGLE_SHOT_NOTICE`) and `ctx orchestrate`'s Claude node launcher
(`src/ctx/orchestrator.py::_launch_host`) now append a short system-prompt
notice, print-mode only, telling the agent plainly that this is a
single-shot run with no supervisor, that no wakeup or notification will
re-invoke it, and that delegating to background subagents means staying in
the turn to collect their results before finishing. It shares the existing
`CTX_WRAP_NO_DISCIPLINE=1` / caller's-own-`--append-system-prompt` opt-out
and is not one of the byte-pinned assets in `tests/test_prefix_stability.py`.
What this proves: the two launch points inject the notice, an interactive
launch does not, and the opt-outs still work (`tests/test_wrap.py`,
`tests/test_task_ledger_orchestration.py`). What it does not prove: that a
real agent reads the notice and stays in its turn. No live re-run of this
round's S6 scenario has happened with the notice in place — that receipt is
still open.

## Harnessed arm, re-run on the fixed tree (2026-09-05)

`python evals/matrix_runner.py --repo . --out /tmp/bb17b --pairs S6:sonnet --arms sj`
on the tree with the print-mode ceiling off (45ab477) and the single-shot
notice in the wrap prompt (fa2bbfd).

| arm | findings delivered | turns | wall | cost | subagents |
|---|---|---|---|---|---|
| harnessed (sj), re-run | 0 compiled; **8 subagent reports, 11 defects** | 31 (cap) | 27.7 min | $34.12 | 8 spawned, **8 completed**, 0 killed |

Proxy scorecard: 1,404 rounds · 549,937 output tokens · 99.0 % cache hit ·
586 invalidations · cold prefix 35,242.

**What the receipt proves.** The main agent read the single-shot notice
and did not repeat the failure: zero wakeup calls, eight subagents launched
and all eight completed, none killed. It then hit the 30-turn cap before it
could compile a ranked report, because it collected the eight background
subagents with fifteen blocking `TaskOutput` calls at one turn each (each
returned when its 600 s block elapsed, so several subagents needed two).
The lifecycle defect is closed; what remains is a turn-budget shape: under
a turn cap, background delegation plus blocking collection costs a turn per
wait. The notice now says so and prefers foreground subagents. The July
table's turn cap (30) was set for single-threaded hunting.

**Findings, read from the eight subagent reports directly** (the main agent
never compiled them). 11 claimed → 11 reproduced → 11 fixed, plus one
sibling found while fixing the first. None of these was in the naive arm's
list; the two arms' finding sets are disjoint. Regression tests:
`tests/test_round17_harnessed.py`.

| # | Defect | Site | Severity | Verified |
|---|---|---|---|---|
| H1 | rollback's restore temp file leaked when its rename failed; fixing it exposed the sibling: the forward commit popped a temp from `staged` before renaming it, so a failed rename leaked that one too | `edit_transactions.py` | low | reproduced (test) |
| H2 | `surface_hide` on a family that was never revealed reported `surface_changed=True` and "hid", so the server emitted a spurious `tools/list_changed` | `surface_gateway.py` | low | reproduced (test) |
| H3 | `ctx get` split the body with `str.splitlines()` while the line index counts only `\n`; a U+2028 inside a JS string made the header say 3 lines and the body print 4, every number after it off by one. Search had already been fixed for this; retrieval and spans had not | `_retrieval/get.py`, `_retrieval/spans.py` | high | reproduced (test) |
| H4 | `ctx get` could not return an empty blob at all: every path refused with "selects nothing" and the refusal suggested `--lines 1:1`, which refused again | `_retrieval/get.py` | medium | reproduced (test) |
| H5 | `doctor_checks` opened three `Store`s and closed none; `ctx mcp` serves `op: "doctor"` from a long-lived process, the exact leak `_WS_CACHE` exists for. The report named two; the schema check was the third | `installer.py` | medium | reproduced (test) |
| H6 | `normalize_targets((".",))` raised `IndexError` (pathlib normalizes `.` to no parts), not the module's own `WorktreeIsolationError`, so a whole-repository scope crashed orchestration instead of being refused | `worktree_isolation.py` | medium | reproduced |
| H7 | every reflex mutator did read → modify → write with nothing held; two hooks from one turn's parallel tool calls both read `commands=5` and both wrote 6, losing interventions and windows. Engagement's ledger already used flock | `reflex.py` | medium | reproduced (fork test, lost updates without the lock) |
| H8 | `note_symbol_grep` kept the first 64 symbols ever seen (`syms[:64]`) so no new symbol was recorded after the 64th and the count froze | `engagement.py` | low | reproduced (test) |
| H9 | `_launch_host` used `subprocess.run(timeout=...)`, which kills the host CLI only; anything it forked (a sandboxed test run, a subagent) outlived the node's timeout, invisible and still writing. `_proc.py` had the group-kill pattern; this call did not use it | `orchestrator.py` | high | reproduced (grandchild test) |
| H10 | a job whose supervisor died before its first state write stayed `launching` forever and `ctx job <id> --wait` polled forever; orphan adoption was gated on `running` | `jobs.py` | medium | reproduced (test) |
| H11 | a bare `ctx job <id>` on a finished background job computed the run's exit code and returned 0 anyway; `ctx run --bg -- false` then reported success | `commands/execute.py` | high | reproduced (test) |

Refuted from the same reports: none. One subagent (digest formatters) found
nothing and said so, listing what it had ruled out.

**Reading it, round 17 as a whole.** Naive arm: 10 verified defects for
$13.80 in one run. Harnessed arm: 0 delivered in the first run (killed by
print mode, $22.72), 11 verified defects in the re-run ($34.12) that the
main agent could not compile. The two finding sets do not overlap, which
says more about the eight-way partition of the tree per arm than about the
harness. The harness's own defects on the way — the print-mode ceiling, the
belief a tool result planted, the worktree-id race CI found — were each
fixed by a session running under it.

## The improvement route, first live run (2026-09-05)

`python evals/improve_route.py --workspace <clone of ec74ccc> --scope src/ctx`
— hunt, verify, harvest, prove as one `ctx orchestrate` route. Record:
[`improve-route-2026-09-05.json`](improve-route-2026-09-05.json) (the three
node yields, the task ledger, the gate, the review).

| node | model | turns | cost | outcome |
|---|---|---|---|---|
| hunt | frontier | 158 | $78.31 | 33 findings in its yield, more in its transcript |
| verify | standard | 138 | $33.17 | 66 claimed, 66 "verified", 0 refuted; 70 tests written, all failing on the unfixed tree |
| harvest | standard | — | unrecorded | 69 of 70 tests made to pass across 48 files; killed at the hour timeout before handing back |
| prove | economy | — | — | never ran: the steward classified the timeout as a transport failure and queued a same-model retry; a watcher stopped the route first |

**Prove, by hand.** On the harvested clone, 69 of the route's 70 tests
pass and the full suite has 3 regressions, all in the command-substitution
family. The gate: **held** — precision 1.00 by the route's own count, suite
not passing. That is the gate working: the verify node's zero refutations
was the number to distrust, and the suite refuted the substitution family
for it.

**Hand review, then harvest.** Every hunk read against its test and the
contract the existing suite pins. Taken: 47 files, 64 tests. Refused: the
four substitution changes (they re-decide the collapse rule rounds 12–14
settled: a bare-identifier hunt collapses to the index-exact `refs`; the
narrower `--glob`/`--include` carry-through is real and deferred), a
redefinition of replay's downstream-fact metric, and a process-wide child
subreaper installed at import time in the process helper (the
killpg-on-timeout-0 hunk it accompanied is taken; the test reads zombie
state instead). One test that cannot force fd recycling reliably is
dropped with its fix kept. Highlights among the taken fixes: the collapse
rewrite could override a secret-path force-ask; `classify_read` resolved a
relative path against the process cwd, not the workspace; the generated
`ctx.toml` pinned 3 of 16 redaction patterns; `ctx seq --keep-going` halted
on a step that failed to spawn; `_authority_ok` failed open on a mistyped
ceiling; two more `splitlines()`-against-the-index sites; a proxy retry
that could pop a second stale pooled connection; `plan_exec` crashing on an
explicit `null` wall budget.

**What the run says about the route.** It found more than the bug-bash
pair did and cost more than both arms together: $111 on the two nodes the
ledger priced, plus an unrecorded harvest. Its estimate was $1.95, built
from placeholder token counts. Three fixes follow: one attempt per node, a
100-turn ceiling per node, and the estimate labelled as a placeholder in
the receipt. A timeout classified as `transient_transport` with a
`retry_same` decision is a steward defect left open here: a node that ran
out of time is not a transport blip.
