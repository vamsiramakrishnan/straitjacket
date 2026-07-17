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

---

# Rematch after v0.4 (substitution steering + logtemplate + wrap + inline widening)

Same task, same model. The harnessed arm now uses `ctx wrap claude` only:
no CLAUDE.md, no manual settings — transparent input substitution
(deny→rewrite), the logtemplate/v1 profile, and result-budget inlining.

## Five-arm results

| Arm | Turns | Cost | Cache-create | Correct |
|---|---|---|---|---|
| **harnessed v0.4, warm cache** | **6** | **$0.072** | 1,840 | ✅ |
| naive, warm cache (matched control) | 9 | $0.186 | 10,042 | ✅ |
| harnessed v0.4, cold cache | 9 | $0.383 | 43,202 | ✅ |
| naive, cold-ish cache | 7 | $0.140 | 7,494 | ✅ |
| harnessed v0.1 deny-mode, cold | 8 | $0.353 | 40,996 | ✅ |

## Findings

1. **Cache-warmth was the dominant confound in the first A/B.** A stray
   smoke test had pre-warmed the naive project's ~34k-token system prefix
   (≈$0.20 of 1h-cache writes) — the original "naive is cheaper" result was
   mostly that. Matched warm-vs-warm reverses it decisively.
2. **Transparent steering beats naive: 6 turns / $0.072 vs 9 / $0.186**
   (2.6× cheaper, 3 turns faster). Zero denials, zero standing prompt text.
3. **Just-in-time protocol teaching works.** With no skill text at all, the
   agent read a digest's `next:` lines and correctly issued
   `ctx get run:<id>#stdout --lines 1:36` unprompted.
4. **Convergence observed in-transcript:** the agent composed naive's best
   pattern (`python3 run.py > log 2>&1; grep -n ERROR log`) as one compound
   command; the hook rewrote it through `ctx run --shell`, and the complete
   bounded result inlined in a single turn — ad-hoc hygiene upgraded to
   provenance-bearing capture at zero marginal cost.
5. **Caveat:** N=1 per arm; naive's 7–9 turn spread across runs shows
   exploration variance. The mechanism-level explanation (no denial
   round-trips, smaller tool results, fewer hops) matches the direction and
   magnitude, but a proper eval should run each arm ≥5×.
