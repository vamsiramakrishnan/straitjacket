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
