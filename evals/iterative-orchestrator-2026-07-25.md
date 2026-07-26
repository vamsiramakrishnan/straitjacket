# Iterative vibe-code — one frontier model vs the routing orchestrator

**Date:** 2026-07-25 · **Runner:**
[`vibecode/iterative_harness.py`](vibecode/iterative_harness.py) · **Task:**
[`vibecode/tasks/triage/`](vibecode/tasks/triage/) · **Aggregator:**
[`vibecode/combine_arms.py`](vibecode/combine_arms.py) · **Raw records:**
`evals/_runs/iter{3,4,5}-*/records.json`

## The task

Single-shot benchmarks ask a model to build an app from a frozen spec. Real
vibe-coding is not that: the design gets **reshaped mid-build**, and the
expensive turn is the one that has to hold everything already built while
reversing the parts the reviewer changed their mind about. `tasks/triage` is
three phases, each graded by headless Chromium:

| phase | what changes | substeps |
|---|---|--:|
| 1 `spec.md` | incident console: table, single-select severity chips, click-to-sort, detail panel, URL-hash sharing | 17 |
| 2 `amend.md` | table → three-lane status board; chips → **multi**-select (reverses phase 1); panel → modal with focus + Escape; persisted theme | 20 |
| 3 `amend2.md` | browser-only → **server API with on-disk persistence**; roving keyboard cursor + status hotkeys + undo; text query composed with chips in the hash; `#visible-count` retired for `#result-summary`; theme becomes a 3-state cycle | 30 |

Each phase re-grades the earlier behaviours its amendment did *not* contradict,
so satisfying a reshape by rewriting from scratch does not get a free pass.
`check_phase3_restart` grades against a **genuinely restarted server**.

All three graders were validated against reference implementations (17/17,
20/20, 31/31) *before* any model money was spent, and re-graded twice to confirm
determinism. Grading a stateful app needed a snapshot/restore around each
attempt so a fix round starts from the built state, not the previous grader's
triage decisions.

## Results

| arm | split | p1 | p2 | p3 | mean | fix | Gemini input tok | billed |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| `solo` | one frontier model does everything | 17/17 | 20/20 | 28/30 | 98% | 0+0+1 | — | **$9.59** |
| `orchestrated` | plan → Opus, build → Sonnet | 17/17 | 20/20 | 28/30 | 98% | 0+0+1 | — | **$7.04** |
| `cross` | plan → Opus, build → Antigravity (Gemini) | 17/17 | 20/20 | 28/30 | 98% | 0+0+1 | 4,248,208 | **$6.70** |
| `cross-sj` | as `cross`, builder shell ctx-contained | 17/17 | 20/20 | 28/30 | 98% | 0+0+1 | 3,913,919 | **$6.61** |

Gemini input tokens are shown only for the Antigravity arms: the Claude CLI
splits its input across uncached / cache-write / cache-read categories, so the
two vendors' token counts are not one column. Cost is the cross-vendor number.

## What it shows

**Splitting plan from build costs nothing in quality and 1.43× less in money.**
Every arm scored 98%. Handing planning to Opus and implementation to Sonnet —
with the plan crossing as a CAS `checkpoint:` rather than as raw prose — landed
the same substeps for **$7.04 against solo Opus's $9.59**. That is the
orchestrator's claim, and on this task it holds.

**The cheapest model per token was not the cheapest arm.** `cross` routes the
build to Gemini 3.6 Flash at $1.25/M in, a fraction of Sonnet's $3/M — and came
within $0.34 of the all-Claude arm, because the Antigravity agent re-sent
**4.25M input tokens to produce 63k of output**. Per-token price is not the
lever; how much context each turn drags behind it is.

**Every arm failed the same two substeps** — the server-restart pair. All four
built status persistence as an in-memory store on the server: the API returns
the updated status, `#result-summary` updates, a page reload holds, and then the
state evaporates when the process restarts. Every check except a real restart
passes. Four independent builds converged on the same wrong thing, which is the
kind of failure only an out-of-band check catches.

**Phases 1 and 2 did not discriminate at all** (17/17 and 20/20, zero fix rounds,
every arm). The reshape only bit at phase 3, where the app had to grow a real
backend. Reported because a two-phase version of this eval would have concluded
"all arms identical" and been useless.

## Honest limits

- **n=1 per arm.** These are single runs of an expensive task; the cost ordering
  (solo > orchestrated) is a large gap, but the `cross` vs `cross-sj` gap is not.
- **`cross-sj` is close to a null result, by construction.** Both Antigravity
  arms run through `agy_build.py`, whose `shell` tool already truncates output
  at 6,000 characters — so there was very little flood left for containment to
  remove. The 8% difference is within what a single run can attribute. The 4.25M
  input tokens are the agent's own accumulating transcript, which a birth gate
  does not address.
- **The Antigravity arms do not exercise the product's host path.** They drive
  the `google-antigravity` **SDK in-process**; they do not launch the `agy` CLI
  through `ctx.orchestrator.launch()`, so they never inherit the workspace hooks
  that `ctx wrap` installs. Containment in `cross-sj` is hand-rolled at the tool
  boundary by the eval, which is why the arm exists at all — in the product,
  containment is ambient per node and would not be an arm.
- **Nor do the Claude arms.** `harness.py:_claude` shells out to `claude -p`
  directly with no `--settings` injection, so all four arms ran *unharnessed*.
  The routing comparison is unaffected (no arm had containment), but none of
  these numbers measure straitjacket's effect on a build.
- **The `agy` CLI cannot be driven headlessly.** It authenticates by interactive
  OAuth browser login and ignores `GEMINI_API_KEY`, so routing the Antigravity
  arms through `launch()` is not possible in a CI-style container regardless of
  installation. This is the blocker to making the arms faithful, and it is
  upstream of this repo.
- A void first run of `cross-sj` (`evals/_runs/iter3-cross-sj`) is **excluded**:
  a router/executor mismatch assigned it Sonnet and then launched the
  Antigravity SDK with the model id `sonnet`, which 404s. Every build node
  failed instantly and the fix round did the whole build, so its cost measured
  nothing. Fixed in the same change that added this receipt.

## Reproduce

```bash
pip install -e '.[dev]' playwright
python evals/vibecode/iterative_harness.py --arm solo --arm orchestrated
CTX_AGY_PYTHON=/tmp/agy-venv/bin/python GEMINI_API_KEY=... \
  python evals/vibecode/iterative_harness.py --arm cross --arm cross-sj
python evals/vibecode/combine_arms.py evals/_runs/iter*-*
```
