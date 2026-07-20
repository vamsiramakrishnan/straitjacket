# Three-arm diagnosis benchmark: naive vs Headroom vs straitjacket — and the skill-delivery gap

**Date:** 2026-07-20 · real coding agents (Claude Code `claude -p`, Haiku
4.5, ≤30 turns), seeded single-bug regression diagnosis. Harness:
[`evals/ask_diagnose_3arm.py`](ask_diagnose_3arm.py); raw per-arm results
+ transcripts: [`ask-diagnose-3arm-2026-07-20.json`](ask-diagnose-3arm-2026-07-20.json).
N=1 per arm (declared caveat — live agents are non-deterministic; see
Caveats). This is not a resolve-rate claim.

## The task

A committed-green order-pricing repo with one seeded regression: a `/ 10.0`
that should be `/ 100.0` inside `apply_discount`, which fails 2 of 5 tests.
The agent must make the suite pass again and name the culprit as
`CULPRIT: <file>:<function>`. The gold culprit
(`orders/pricing.py:apply_discount`) is known, so grading is **model-free**:
`success = suite green after AND culprit correctly named`. This is the
task `ctx ask --intent diagnose` / `ctx q 'fails last | in-changed'` exist
to collapse — deliberately chosen as straitjacket's *hardest* case for a
containment win (a small repo with no flood: there is almost nothing to
contain, so mediation is close to pure overhead).

Arms (identical task, model, cap; isolated `CLAUDE_CONFIG_DIR` each):
`naive` = plain `claude -p`; `headroom` = `headroom wrap claude` (0.32.1,
unidiff-tier fallback — its `tokensave` binary 403'd on download, as in the
prior headroom receipts); `sj` = `ctx wrap claude --proxy` (the shipped
Claude Code harness: hooks + proxy + explorer); `sj_skill` = `sj` **plus**
the ctx verb card placed in the fixture's `CLAUDE.md` — the probe for the
skill's marginal effect.

## Results (clean run)

| | naive | headroom | sj | sj_skill |
|---|---|---|---|---|
| Suite green after | ✅ | ✅ | ✅ | ✅ |
| Culprit named | ✅ | ✅ | ✅ | ✅ |
| Turns | **8** | 7 | 9 | 12 |
| Cost (all forks) | **$0.045** | $0.073 | $0.064 | $0.091 |
| Wall clock | **34 s** | 53 s | 36 s | 53 s |
| Output tokens | 2 116 | 2 517 | 2 129 | 2 563 |
| Cache-read tokens | 167 k | **120 k** | 304 k | 536 k |
| `ctx ask` / `ctx q` used | – | – | **0 / 0** | **1 / 1** |

## The two findings

**1. On a no-flood task, context mediation is net overhead — bounded, and
expected.** All four arms solved it identically; naive was cheapest and
fastest. Containment added +40 % (sj) to +102 % (sj_skill) cost over naive
— on a task that costs pennies. This is exactly the *low-complexity
regime* the project's own ship gate caps (`overhead ≤ 5 %` is asserted for
tasks where containment cannot pay); a single-bug fix in a 4-file repo is
below that line by construction. straitjacket's measured wins live on
floods and long sessions (coverage-corpus: 8×–151× collapse;
plan-collapse: 6 rounds → 1), not sub-$0.10 point-fixes. Publishing this
honest loss is the point — a containment layer that *also* helped here
would be suspicious.

**2. The skill vocabulary changes agent behavior — but only when it
reaches the agent, and `ctx wrap claude` does not deliver it.** The bare
`sj` arm invoked **zero** `ctx ask`/`ctx q` (it used `find`/`pytest`/`python`
directly). The `sj_skill` arm — identical except the verb card sat in
`CLAUDE.md` — invoked `ctx ask` **and** `ctx q`. So even Haiku reaches for
the shipped question verbs when they are in its context; the gap is purely
*delivery*. `ctx wrap claude --proxy` injects hooks, the proxy, and the
explorer agent, but has **no channel that surfaces the skill vocabulary**
to a Claude Code agent (the skill is delivered for Antigravity, where the
host reads `skills/`; SessionStart on Claude Code injects the MCP-surface
advisory, not the verb card — matching the standing `hook.py` note "no
teaching surface on this host"). The verbs shipped across four waves
(`q`, `investigate`, `ask`, `corpus`, `records`); the surface that teaches
a Claude Code agent to invoke them did not.

## What shipped from this receipt

- **The skill caught up to the engines**: `SKILL.md`, `references/verbs.md`,
  and the Codex `AGENTS.md` block now teach `ctx ask` / `q` / `investigate`
  / `corpus` / `records` (they stopped at the pre-M-J `run/search/get/stats`
  vocabulary).
- **Claude Code delivery closed**: `install_claude` (the persistent Claude
  Code install) now upserts a compact ctx verb card into the workspace
  `CLAUDE.md` — marker-delimited and versioned, mirroring the shipped Codex
  `AGENTS.md` block — so the vocabulary the eval proved drives adoption
  actually reaches the agent. A persistently-installed workspace now
  behaves like the `sj_skill` arm; `ctx wrap` stays ephemeral (zero
  residue) and relies on the same card if the workspace was installed.
- **A reusable 3/4-arm harness** (`ask_diagnose_3arm.py`) with a model-free
  grader and a transcript-derived vocabulary-adoption counter.

## Caveats

N=1 per arm; a first, contaminated run (system `python3` lacked pytest → every
arm detoured through `pip install pytest`; an accidental float-precision
second bug in `order_total`) was **discarded**, not reported — this receipt
is the clean re-run (pytest provisioned for the agents, single exact bug).
Haiku only; a stronger model and a flood-bearing task are the natural next
arms (the verbs' payoff needs a task large enough to amortize them —
`sj_skill` here took the `ask`/`q` path on a problem that direct read+edit
already solved, which *costs* turns rather than saving them). The
adoption-vs-payoff distinction is the open question: this run shows the
verbs are *adopted* once surfaced; whether they *win* needs the flood case.
