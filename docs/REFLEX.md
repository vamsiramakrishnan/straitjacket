<img src="../assets/readme/docs/reflex.svg" width="100%" alt="Reflex — closed-loop conditionality. Architecture wave, doc 2 of 4."/>

<sub><a href="README.md">« straitjacket / docs</a></sub>

# Reflex: closed-loop conditionality

**Date:** 2026-07-18 · design doc for the intelligence layer, written
against the spec3 receipt (`evals/spec3-haiku-2026-07-18.md`): every
conditional fired to spec, and the system still hit the turn cap at
2.7–3.8× naive's cost. The failure class this doc addresses:
**conditionality aimed at the wrong axis** — and the reason it happens.

## Why we fired on the wrong axis

Every ladder we own is an **open-loop controller**: a signal chosen at
design time (output size, exit code, window %, call count) drives a
response chosen at design time (budget multiplier, steering strength,
hint cap). Open-loop control is exactly right when the axis is known —
flood prevention is a solved axis precisely because we picked it and
instrumented it.

But an open-loop system cannot notice that its intervention is *failing*,
because nothing measures the intervention's effect — only its trigger.
In spec3 the system had every signal needed to see its own failure in
real time: the same pytest command re-issued 8× with slicing pipes,
16 retrieval hints emitted and 0 followed. No component consumed those
signals, because they are **behavioral responses to our own output**, and
no layer watches that.

The fix is not smarter axis-picking (the next unpicked axis will get us
again). It is closing the loop.

## The principle

> **Every intervention is a hypothesis about the model's next action.**
> A digest hypothesizes "the model will use this instead of re-running."
> A hint hypothesizes "the model will resolve this address." A denial
> hypothesizes "the model will take the remediation." Intelligence is
> measuring each hypothesis *per event* and adapting on the axis the
> evidence names — not the axis we guessed at design time.

Corollary (the asymmetric loss prior): under-containment costs tokens —
cheap, bounded, recoverable next turn. Over-containment costs turns and
attention — expensive and compounding (spec3: ~2k tokens saved per view,
20+ turns lost). Reflexes therefore only need to be *fast* in one
direction: detecting that an intervention starved the model.

## The four layers

### 1 · Intent declaration (mechanisms say what they expect)

Each intervention class carries a machine-checkable expected-response,
an enum not an essay:

| intervention | hypothesis: next action is NOT |
|---|---|
| run digest with omissions | re-running the same command (± slicers) |
| emitted `next:`/span hints | ...ignored forever while re-runs happen |
| deny + remediation | the same command retried verbatim |
| rewrite steering | the model routing around `ctx` entirely |

This costs nothing at runtime — it is a classification the reflex arc
(layer 2) applies when it sees the *next* command.

### 2 · The reflex arc (deterministic behavioral detectors)

The PreToolUse hook already sees every command in order. A small set of
detectors — string/signature level, same discipline as the classifier —
score each prior intervention against its declared intent and append one
fail-open ledger line (the adoption-ledger pattern, generalized):

- **starvation**: same command signature re-issued after its digest,
  especially with slicing decorations (`| head`, `| tail`, `--tb=`,
  `| grep`) — the spec3 loop, detected at occurrence 2, not at benchmark
  time.
- **affordance failure**: N hints emitted for a handle, zero `ctx get`/
  `ctx search` on that handle before the next re-run.
- **friction loop**: deny → verbatim retry; rewrite → workaround.
- **landings** (the positive class): span resolved, hint followed,
  remediation taken — reflexes must be earned off, not just on.

### 3 · In-session reflexes (fast loop: adapt on the named axis)

When a detector fires, escalate **on the axis the detector names**,
deterministically, with the change declared in the output:

- starvation on a command family → that family's next digest *densifies*:
  spend the (already-doubled) failure budget on the full census —
  every failing test name, every diagnostic — instead of first-failure
  detail; swap hints for inline evidence. Header declares it:
  `densified: re-run detected`.
- affordance failure (lean reader) → stop paying for hints; inline the
  evidence the hints pointed to. This is the spec3 inversion made
  mechanical: **strong models follow addresses; starved models need the
  evidence delivered.** Model tier is the prior; measured follow-through
  is the update.
- friction loop → escalate teaching once, then stand down for that
  signature this session (a guard that loses twice stops paying the
  denial round-trip — availability over purity, logged as an outcome).
  **Scope correction (EDC §11): stand-down applies to the evidence
  plane only — discretionary steering and containment may concede.
  Safety-class denials (secret paths, confinement, authorization) sit
  outside the adaptive system and never stand down, regardless of
  repetition.**

Reflex state is session-scoped, latching per command-signature, and a
pure function of the session's command sequence — same transcript prefix,
same reflex state, so determinism survives: content identity never
depends on reflex state; only *which* deterministic rendering was chosen,
and that choice is declared in the digest header and recorded in digest
meta.

### 4 · The slow loop (epochs learn the starting point)

Outcome events are exactly the training data the policy-epoch compiler
was built for. Compiled offline, reviewed, committed — never runtime ML:

- per command-family × reader-tier: starting digest density, hint count,
  steering strength (start dense for pytest-under-haiku if that is what
  the ledger keeps saying — the reflex then has nothing to correct).
- guard-mode outcome accounting (LADDERS edge 6) falls out for free: the
  same ledger scores allow/deny/rewrite decisions by what followed them.

## Axis discovery (catching the *next* wrong axis)

The reflex arc watches known hypothesis classes. For unknown axes, the
instrument is the **loop detector in the scorecard**: `ctx stats
--session` grows a behavioral-anomalies section — repeated command
signatures, interventions with zero landings, thrash sequences — computed
from the same ledger. The eval harness rule becomes: *every benchmark
publishes its transcript-anomaly audit alongside its table* (spec3's
diagnosis section, made mandatory and mechanical). Anomalies that recur
across sessions get filed as debt with coordinates — the bench → anomaly
→ debt pipeline is how a new axis earns instrumentation before it earns
a mechanism.

## What this is not

No runtime model calls to decide policy. No nondeterministic digests. No
per-request adaptivity invisible to the reader — every reflex adaptation
is declared inline and recorded in telemetry with the signal that caused
it (the LADDERS receipts rule, applied to the reflex layer itself).

## The design rules (adopted 2026-07-19, statuses against the codebase)

| # | rule | status |
|---|---|---|
| 1 | Census before detail | ✅ shipped: lint/v1, pytest/v1 (budget priority enforced) |
| 2 | Semantic coverage before token savings | ✅ doctrine (quiet-needle; logtemplate) |
| 3 | Profiles extract facts; policy chooses presentation | 🔨 the generalization refactor: `extract() → Facts` + one presentation layer; densify/census/tier become inherited properties, not per-profile code |
| 4 | Model tier is a prior, not a verdict | ✅ doctrine; the measured-follow-through update is wired for hints, not yet for tier |
| 5 | Rerun equivalence must account for source changes | ⚠️ v2 shipped the event form (Edit disarms); v3 is the content form — compare `source.worktreeHash` (already minted per run): equal hash = starvation regardless of Edit events |
| 6 | Compression gets a circuit breaker | 🔨 densify exists; the concession state doesn't: after N post-densify starvations, capped raw passthrough (bounded by failure budget — Gate 4 holds) |
| 7 | Safety never adapts away | ✅ structural; harden with an invariant test: secret/escape guard decisions byte-identical under any reflex/policy state |
| 8 | Optimize total downstream cost, not output tokens | ✅ doctrine (spec3's 2k-tokens-vs-20-turns); scorecard prices rounds |
| 9 | Measure interventions by progress, not hint clicks | 🔨 the census enables it free: failure-count trajectory per signature (8→5 = intervention worked) — the correct [digest_density] training signal |
| 10 | Fix pytest/v2 before the general controller | ✅ sequencing adopted: pytest/v2 (rules 5v3+6+9) under the n≥3 referee, then the rule-3 split carries go test/jest/cargo/build in one move |

Why pytest first, generalized second: mechanisms follow receipts — pytest
is where the benchmark bled. The reflex machinery is already tool-agnostic;
only *dense renderings* are profile-specific, and rule 3 is the one-refactor
path that gives every profile census/densify/breaker at once. Doing the
split with exactly one proven Facts shape would be a guess; doing it after
pytest/v2 preserves three validated behaviors.

## v0.21 deliverables and acceptance

1. `pytest/v1` failing-test census (debt 74db82e027) — the dense form
   layer 3 escalates to; useful standalone.
2. Reflex arc v1: starvation + affordance detectors, outcome ledger,
   densify-on-starvation for run digests; scorecard anomalies section.
3. Slow-loop schema: outcome events into `ctx policy compile` (epoch
   table for digest density per family × tier).

**Acceptance gate (frozen referee):** re-run `evals/spec3_runner.py`
unchanged. Pass = sj turns collapse toward naive's (≤1.5×) on both tasks
with holdout still 16/16 and cache hit ≥ naive's; the transcript audit
must show the re-run loop gone (starvation detector may fire once — the
reflex correcting is the mechanism working; firing eight times is the
bug we had). Flood-containment evals must not regress (needle-drop,
eval-collapse S-C).
