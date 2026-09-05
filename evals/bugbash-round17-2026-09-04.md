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
