# Three-arm overhaul benchmark: naive vs straitjacket vs Headroom, on straitjacket itself

**Task** (identical, Sonnet 5, ≤60 turns, full edit tools): tech-debt +
documentation + DevEx overhaul of this repository — baseline the suite, add
CI (3.11/3.12 + ripgrep), write CONTRIBUTING.md, fix ≥3 debt items in
src/ctx, re-run green, report. Arms: plain `claude -p`; `ctx wrap claude`
(isolated CLAUDE_CONFIG_DIR); `headroom wrap claude` (proxy + serena +
tokensave stack, chained through the environment's auth proxy).

## Results

| | naive | straitjacket | Headroom |
|---|---|---|---|
| Task complete | ✅ | ✅ | ✅ |
| Suite green after edits | ✅ | ✅ | ✅ |
| CI valid (matrix + rg) | ✅ | ✅ | ✅ |
| CONTRIBUTING.md | 5.0 KB | 5.9 KB | 3.9 KB |
| src/ctx files fixed | 4 (+7/−10) | **6 (+37/−41) + tests added** | 3 (+9/−21) |
| Turns / wall clock | 59 / 7.3 min | 51 / **4.1 min** | 77 / 9.6 min |
| Cost (all models, incl. forks) | **$2.52** | $8.71 | $3.15 |
| Cache-create (main loop) | 62.8k | 35.4k | **227k** |
| Guard denials | – | 1 | – |

## Verdict on the headline question

**No quality degradation from context mediation in any arm.** All three
produced complete, correct deliverables with green suites. Depth ordering:
straitjacket > naive ≥ Headroom — the harnessed arm removed the most dead
code (net −4 lines across 6 modules), was the only arm to add tests for its
fixes, and wrote the deepest debt report (vestigial Guard fields, dead
deny_globs config surface, duplicated literal defaults — all verified
correct). It also dogfooded unprompted: retrieved its own captured pytest
output via `ctx get run:<id>` and delegated the debt hunt to a sub-agent.

## Cost analysis — the interesting part

- **Headroom's per-request rewriting tax is real**: 227k cache-creation vs
  naive's 63k (3.6×) — compression saved content tokens, then repaid them in
  prefix-cache churn, netting +25% total cost over naive at equal quality,
  plus the slowest wall clock (proxy latency + analysis-heavy style: 24
  whole-file reads, 22 single-symbol greps, 1 edit by call 57).
- **straitjacket's main loop was the leanest of all three** (35k
  cache-create, smallest per-turn context, fastest wall clock) — but its
  *choice* to fork a research sub-agent drove 19.6M aggregate cache-reads
  ($5.87), making it 3.5× naive's cost. That is quarantine's documented
  trade (ROADMAP M-A: "burns tokens in the fork"), not harness overhead:
  the fork bought the depth advantage and the 4.1-minute wall clock.
  Making forks cheap (digest-fed explorers, checkpoint-shaped reports,
  cheap-model forks with handle citations) is precisely roadmap M-A + the
  cascade-safety mechanism.

## Caveats

N=1 per arm; sj + Headroom ran concurrently (durations share bandwidth);
the first sj run was discarded after Headroom's globally-registered MCP
servers contaminated it (fixed via CLAUDE_CONFIG_DIR isolation — lesson:
isolate, don't serialize); Headroom ran its full intended stack including
serena/tokensave MCP, which shares credit/blame for its profile.

---

# v0.6 rematch (2026-07-18): the cost sign flips

Same task on the grown v0.6 repo (168 tests), naive vs `ctx wrap` with the
four new mechanisms (explorer agent, read-budget governor, ctx map,
ctx diff), both arms parallel from the start in isolated CLAUDE_CONFIG_DIRs.

| | naive | straitjacket v0.6 |
|---|---|---|
| Cost (all models, incl. forks) | $3.70 | **$2.21 (−40%)** |
| Wall clock | 7.2 min | **6.1 min** |
| All-models cache-create | 222,605 | **86,185** |
| All-models cache-read | 6.97M | **4.59M** |
| Deliverables (CI/CONTRIBUTING/3 fixes) | ✅ all | ✅ all |
| Suite green after edits | ✅ | ✅ |
| Terminal | max_turns (no final report) | max_turns (no final report) |

## Findings

1. **The fork externality is gone — and switched sides.** Round 1's
   harnessed arm burned $5.87 in an ungoverned research fork. In the
   rematch the harnessed arm's all-models usage equals its main loop
   exactly (no runaway delegation), while *naive* forked expensively this
   time (222k vs 70k main-loop cache-create; +2.4M fork cache-reads).
   Mechanism evidence in the harnessed transcript: a `ctx map` orientation,
   six `ctx get` retrievals, and one read-budget pressure event — orient
   cheaply, retrieve exactly, never wander.
2. **Round-over-round: harness $8.71 → $2.21 (−75%) while naive rose
   $2.52 → $3.70** on the larger repo. The v0.6 mechanisms account for the
   harness delta; the task simply got harder for everyone else.
3. **Quality parity maintained** (all deliverable gates pass both arms);
   both hit the 60-turn cap before writing final reports — the task has
   outgrown the cap on the bigger repo; future runs should use ≤80 turns.
4. Caveats: N=1 per arm; fork/no-fork carries agent-choice variance; both
   arms share the max_turns truncation.
