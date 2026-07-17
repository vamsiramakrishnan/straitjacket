# A/B: harnessed vs naive Claude Code on a buried-evidence debugging task

**Date:** 2026-07-17 · **Model:** claude-sonnet-5 (headless `claude -p`, max 30 turns)
**Task:** find the root cause of 1 failing request out of 20,000 in an
integration run that emits ~868 KiB (~222k est tokens) of log; evidence is a
2-line ERROR at line 14,238. A stray `* 10` in `svc/retry.py:compute_backoff_ms`
blows a 300ms deadline budget.

Harnessed variant: `.claude/settings.json` PreToolUse hook
(`ctx hook claude-code pre-tool-use`) + the ctx skill as CLAUDE.md.
Naive variant: same project, no hook, no CLAUDE.md.

## Results

| Metric | Harnessed | Naive |
|---|---|---|
| Correct root cause | ✅ | ✅ |
| Turns | 8 | 7 |
| Wall clock | 36s | 24s |
| Output tokens | 1,725 | 1,322 |
| Cache-creation tokens | 40,996 | 7,494 |
| Total cost | $0.353 | $0.140 |
| Guard denials | 1 (compound `which; ls`) | n/a |
| Raw log bytes in transcript | 0 | 0 |

## What each agent actually did

Harnessed: guard redirected its first compound command → `ctx run --shell`;
read the two small source files natively (allowed); `ctx run --focus` on the
integration script → 150-token digest; one batched 3-pattern `ctx search`
→ answer. Textbook protocol, zero floods, evidence cited by handle.

Naive: read the source files, then **independently invented the harness
workflow by hand** — redirected the run to a file (`python3 run.py > /tmp/out.log`)
and `grep -n`'d it (252 bytes into context). Zero floods, correct answer.

## Honest conclusions

1. The plugin works end-to-end under real Claude Code: the hook fires, denies
   with an executable remediation, the model adapts on the next turn, and
   answer quality is unimpaired.
2. On a small, clean task with a frontier model, the harness *costs* more
   than it saves (≈ +$0.21): skill tokens, one denial round-trip, and digest
   scaffolding, against a baseline where Claude Code's built-in ~30k-char
   Bash truncation plus a disciplined model already avoided the flood.
3. The naive agent's redirect+grep is ad-hoc self-restraint: no snapshot, no
   provenance, no cross-turn handle, no budget, and nothing stops the *next*
   (or a weaker) model from `cat`-ing the file. The harness converts a good
   model's best-case habit into every model's worst-case guarantee.
4. Where the harness wins on cost, not just discipline: sessions with many
   noisy commands (N × truncated dumps persist in context every following
   turn and force compaction), artifacts re-referenced across turns/sessions,
   and evidence past the truncation horizon that in-band output would lose.
5. Measured harness overhead for the full evidence workflow
   (run digest + search + get): **456 model-visible tokens** vs ~222,000 raw
   — a 487× reduction on first exposure, before any multi-turn amplification.
6. Friction to tune: the first denial hit an innocuous `which ctx; ls -la`
   compound; chains of all-bounded commands should classify as bounded.
