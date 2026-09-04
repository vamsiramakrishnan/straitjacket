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
