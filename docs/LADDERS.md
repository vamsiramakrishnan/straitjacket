<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs/ladders.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs/ladders-light.svg" width="100%" alt="Ladders — the conditionality audit. Architecture work, doc 1 of 4.">
</picture>

<sub><a href="README.md">« straitjacket / docs</a></sub>

# Ladders: the conditionality audit

> **Design & internals — not product documentation.** This doc explains *why* a
> mechanism exists and how it was reasoned out; it may describe an idea before it
> ships or record one that was rejected. For what the product does **today**,
> prefer [`spec/`](../spec/) and the [changelog](../CHANGELOG.md), and read any
> status label literally. New to the vocabulary? Read
> [How it works](HOW-IT-WORKS.md) and [Concepts](CONCEPTS.md) first.
>
> This is an internal audit that lists known rough edges and design debt by
> source coordinate — read those as engineering notes, not as product behaviour.

**Date:** 2026-07-18 · analysis pass over every tiered/conditional construct
in the product — how each is traversed, whether they stack, where the rough
edges are, and the one change that brings them together. House criterion
applied throughout: **a conditional is only as good as its measurement** —
every branch must emit a telemetry event carrying (signal, branch taken),
or it cannot earn (or lose) its place in a policy epoch.

## 1 · The ladder registry

Every ladder in the system, by axis. "Traversed by" says who moves along
it; "latching" says whether it can flap.

| ladder | axis | steps | traversed by | signal | latching | measured today? |
|---|---|---|---|---|---|---|
| Solution ladder (skill r13) | what code to write | not-needed → reuse → native feature → stdlib → one-liner → new code (exempt: trust boundaries, data loss, security, accessibility) | model (advisory) | none (in-prompt) | n/a | ✅ deliverable metrics, A/B-adopted |
| Capture ladder | how work executes | native → `run` → pipeline (`--shell`) → `seq` → `eval` → `job` (this wave) | model (taught) + hook (steered) | command shape | n/a | ⚠️ adoption ledger just landed (eval wave); no per-step traversal metric |
| Emission budgets | how much rides | inline-complete → digest → failure×2 → truncation note | harness | output size, exit code | no (per event) | ✅ telemetry raw/emitted per verb |
| Graduated engagement | affordance surface | passive → active | harness | call count ≥ 8, truncation events, model tier | latches up per session | ⚠️ state file exists; transitions not telemetried |
| Window pressure | residency defense | normal → tightened budgets → epoch-latched rescue | harness (hook + proxy) | window.json fullness % | rescue latches | ✅ scorecard (rescue round, blocks elided) |
| Guard modes | steering strength | advisory → guarded → strict; allow → ask → force_ask → deny | static config | none at runtime | static | ❌ no per-mode outcome data |
| Policy epochs | per-command trust | unknown → promoted / demoted | compiler (offline) | run telemetry: bounded-output reliability | committed epochs | ✅ by construction |
| Deployment tiers | enforcement depth | skill → plugin → native → hardened | human | none | static | n/a |
| Model tiers (Maki steal, open) | who reasons | lean → strong | not built | task scale | — | — |

Two observations fall out of just writing the table:

1. **Three different traversers.** Model-traversed ladders (solution,
   capture) are advisory and measured only at the deliverable; harness-
   traversed ladders (budgets, engagement, pressure) are enforced and
   partially measured; statically-set ladders (guard modes, deployment)
   are never traversed at runtime and generate **no outcome data at all** —
   we cannot say whether `strict` beats `guarded` because nothing records
   what each mode cost or saved.
2. **Latching discipline is inconsistent.** Rescue latches (deliberately,
   to protect the cache prefix); engagement latches up; pressure
   tightening un-latches freely (flaps with window %); failure budgets are
   memoryless. Nobody wrote down which behavior each axis *should* have.

## 2 · Do they stack? (mostly orthogonal, one real conflict, one gap)

The ladders live on different axes, so in principle they compose
multiplicatively: a *failing* run (×2 budget) under *high pressure*
(tightened budgets) in a *lean-model* session (suggestions capped) should
get all three adjustments at once. In practice:

- **They never meet.** Window pressure is applied only inside the hook
  (`_apply_window_pressure`, hook.py) to *native-read* budgets; the CLI
  verbs (`run`/`seq`/`eval`/`get`/…) size their digests from `ctx.toml`
  budgets alone — a digest rendered at 92% window fullness is exactly as
  large as one at 10%. The two halves of the same defense don't talk.
- **The one real conflict is unadjudicated:** failure asymmetry says
  *grow* (×2, failure is evidence); pressure says *shrink*. Today they
  can't collide (different layers), but the moment pressure reaches CLI
  budgets, the composition rule must be explicit. Proposed: multiply
  modifiers, floor at a minimum evidence budget — a failing test's
  traceback must never be squeezed below usefulness, because failure
  evidence is the single highest-value byte class we emit (the whole
  failure-asymmetry doctrine).
- **Inconsistent application is the quiet stacking bug:** the engagement
  filter (`filter_digest`/`suggestion_cap`) runs on `run` and `eval`
  emissions but **not** on `seq` (cli.py: seq prints `bounded()` only) —
  so a lean-model session still pays for `next:` teaching lines on every
  seq digest. Same class: MCP's dispatch handles `callers/callees/impact`
  but the tool schema's `op` enum doesn't declare them (and `diff` isn't
  reachable via MCP at all) — the ladder of "what the model may call"
  disagrees with "what the server does."

**Verdict: stackable by design, unstacked in implementation.** Nothing is
mutually exclusive; each ladder guards a different axis. What's missing is
a single point where they compose.

## 3 · Bringing them together: signals → one resolver → receipts

The cohesive shape (small, mostly wiring, no new concepts):

1. **A signal record.** One tiny struct assembled per emission:
   `(window_pct, is_failure, engagement_state, model_tier, task_scale)`.
   Every input already exists (`window.json`, exit code, engagement state
   file, proxy model id); nothing new is measured.
2. **One choke point.** A `resolve_budget(base, signals) -> (budget,
   applied)` helper that every CLI emission path calls where it currently
   computes `budget` by hand (run, seq, eval, retrieval, diff, map, code
   verbs — seven call sites, all currently slightly different). The
   modifier table (failure ×2.0, pressure ×0.6 above threshold, lean-model
   suggestion cap, evidence floor) lives in config with defaults —
   **and can be overridden by a committed policy epoch**, which is where
   learned thresholds go (ctx-policy.toml already has the promote/demote
   machinery; budgets modifiers are the same shape: telemetry → compile →
   commit → enforce).
3. **Receipts per branch.** `resolve_budget` appends `applied` (the list
   of modifiers that fired) to the existing telemetry event. That single
   field makes *every* conditional measurable: `ctx gain` can then report
   "pressure-tightening fired N times, saved ~X tok, preceded rescue in
   Y% of sessions" — the data the guard-mode ladder has never produced.
4. **Model-traversed ladders keep their nature but gain instruments.**
   The solution and capture ladders stay advisory (that's their A/B-proven
   form); their instrument is the adoption ledger pattern this wave
   introduced for eval — extend the same `*_opportunity` counting to seq
   (chains of ≥3 mechanical Bash rounds = a seq opportunity) so ladder
   traversal becomes a ratio per session, not an anecdote.

Latching rule, made explicit while we're here: **defensive ladders latch
(rescue, engagement), economic ladders flap freely (budgets), evidence
floors never yield.**

## 4 · Rough-edge inventory (fresh findings; S5/S6 debt not re-listed)

| # | edge | coordinates | class |
|---|---|---|---|
| 1 | seq emission skips engagement filter — lean sessions pay for `next:` hints run/eval strip | src/ctx/cli.py seq branch | inconsistency |
| 2 | window pressure never reaches CLI digest budgets (hook-only) | src/ctx/hook.py `_apply_window_pressure` vs cli.py budget sites | missing composition |
| 3 | MCP op enum omits `callers/callees/impact` (dispatched but undeclared) and `diff` (not dispatched) | src/ctx/mcp.py TOOL_SCHEMA vs `_dispatch` | surface drift |
| 4 | `run` denies failure budget to timeouts (`exitCode None`); `eval` grants it (124) — same event class, different budgets | src/ctx/cli.py `_cmd_run` vs `_cmd_eval` (S6 debt 135d7df383 adjacent) | inconsistency |
| 5 | engagement transitions (passive→active) not telemetried — the graduation mechanism can't prove it graduates at the right time | src/ctx/engagement.py `note_call` | unmeasured conditional |
| 6 | guard modes / unknown_command policy produce no outcome telemetry — mode choice is faith, not measurement | src/ctx/hook.py `_load_guard_policy` | unmeasured conditional |
| 7 | `next:` teaching lines: emission counted, follow-through not — can't tell if hints are load-bearing or tax | telemetry schema (retrieval.record_telemetry) | unmeasured conditional |
| 8 | seven hand-rolled budget computations across cli.py/mcp.py drift independently (run vs eval vs seq vs digest_output each slightly different) | cli.py, digest/__init__.py | duplication → resolver |

## 5 · Measurable-conditional candidates, ranked

Ranked by (evidence already in hand) × (cheap to instrument):

1. **Pressure-aware digest budgets** (edge 2) — signal exists per session
   (window.json), resolver makes it one multiplier, scorecard already
   measures rescue; success metric: rescue onset delayed / fewer blocks
   elided at equal correctness. Eval design: replay overhaul benchmark
   with/without the multiplier.
2. **Timeout failure-budget parity** (edge 4) — one-line class of fix
   inside the resolver; measured by the existing failure-evidence checks.
3. **Hint follow-through** (edge 7) — count `next:`-line emissions and
   subsequent `get/search` on the same handle within the session;
   conditional: stop emitting hints to sessions that never follow them
   (extends the lean-model cap from static to learned). Pure telemetry,
   zero risk.
4. **Seq-opportunity adoption** (§3.4) — same ledger pattern as eval
   adoption, feeds the capture-ladder ratio.
5. **Learned engagement threshold** (edge 5) — telemetry the transitions
   now; compile `activate_after_calls` from session outcomes later
   (policy epoch, same promote/demote shape).
6. **Guard-mode outcome accounting** (edge 6) — record per-decision mode +
   outcome (allowed→flooded? denied→remediation-followed?); after a few
   weeks of epochs, the advisory/guarded/strict choice becomes a measured
   recommendation per repo.

## 6 · What this does NOT propose

No new ladder. No runtime ML. No per-request adaptivity beyond the signal
record (determinism holds: same bytes + same signal record → same digest;
the signal record's inputs are already session state, and `applied`
modifiers are declared in the digest's telemetry, never hidden). The whole
proposal is: **the ladders already exist and already compose in doctrine —
give them one resolver, one receipt stream, and let the existing policy-
epoch loop do the learning it was built for.**
