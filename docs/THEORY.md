<sub><a href="README.md">« straitjacket / docs</a></sub>

# The objective: what straitjacket optimizes, stated once

**Date:** 2026-07-19 · This document states the system's objective function,
labels every shipped mechanism as *derived* from it or *empirically adopted*
under it, and names the measured quantity that scores how close the system is
to optimal. It exists so the answer to "is this principled or a mishmash?" is
inspectable: one objective, two enforced theorems, one regression-gated gap
metric — and an honest list of what is empirical.

## 1. The objective

The transcript is a paid channel. For each tool output `X`, the harness must
choose a representation `T̃` to place on that channel. The objective is the
**information bottleneck** with a **lazy-lossless constraint**:

```
minimize   rate(T̃)  −  β · I(T̃ ; Y)          (per output X)
subject to X │ T̃  stored addressably           (every omitted byte keeps
                                                a resolvable address)
```

where `Y` is the task-relevant variable (which line is anomalous; which test
failed and why; what changed), `rate(T̃)` is the digest's token cost — paid
**every turn** by residency, not once — and the constraint is what
distinguishes this system from every lossy neighbour: distortion is never
destroyed, it is *priced*. The result is a **successive-refinement code**:

- a low-rate always-on layer — the digest, deterministic and bounded;
- a high-rate on-demand layer — `ctx get`, paid only when the model's
  posterior over `Y` says the residual is worth one hop.

`β` is not a constant: failure outputs get `failure_budget_factor ≈ 2×` the
digest budget because a failure's `Y` carries more information than a
success's ([`ctx.toml`](../src/ctx/installer.py) template). That is the
objective showing through the configuration.

## 2. What is proven (enforced theorems)

Two properties are theorems with executable enforcement, not aspirations:

| Theorem | Statement | Enforcement |
|---|---|---|
| **Determinism** | identical bytes → byte-identical digest; the injected prefix is golden-hashed | `PREFIX_VERSION` contract + determinism suite ([`tests/`](../tests/)) |
| **Single refinement boundary** | in the `ctx q` algebra, raw bytes materialize at most once, and only terminally; closure is a total function of the type signature | [DIGEST-CLOSURE.md](DIGEST-CLOSURE.md) + [`tests/test_digest_closure.py`](../tests/test_digest_closure.py) |

## 3. What is measured (the frontier gap)

The objective's optimum — `T̃` a **minimal sufficient statistic** for `Y` —
is not assumed. It is scored, per profile, from recorded sessions:

**Evidence regret** (`ctx replay --regret`,
[`src/ctx/replay.py`](../src/ctx/replay.py)). For every recorded call whose
output the model *provably used downstream* (fragments reused in `Edit`,
coordinates and test node-ids reused in later commands):

```
oracle  = tokens of exactly the used facts        (lower bound on true sufficiency)
actual  = digest tokens + Σ hop-price(one-hop facts)
R       = actual − oracle                          (upper bound on the true gap)
frontier = oracle / actual ∈ (0, 1]                (1.00 = on the frontier)
```

Three properties make R honest rather than decorative:

1. **One-sided.** The facts proxy proves a *subset* of what the model needed,
   so the oracle is a lower bound and R is an **upper bound on the true
   gap** — the metric can accuse the system falsely, never absolve it.
2. **Partitioned.** Calls with no provably-used facts are reported as
   *unattributed digest spend*, never folded into R: the proxy is blind to
   conclusion-shaped evidence ("all tests passed"), and a zero oracle there
   would drown the signal in false regret.
3. **Same-population naive comparison.** `naive-R = raw − oracle` is computed
   only over calls whose raw bytes the transcript actually holds;
   already-harnessed calls (raw in the store, counterfactual unknowable)
   render `—`, never a self-comparison.

First measurement (spec3 archives, 8 sessions): `pytest/v1` — 199/199
downstream-used facts inline (sufficiency holds after profile changes: the
regression gate), frontier **0.17** — the digest spends ~5.7× the
strictly-used evidence. That 0.17 is the honest, load-bearing number: it is
where "are we near-optimal" stops being rhetoric. Read with care — the oracle
is a lower bound, and censuses the model *reads* without *reusing verbatim*
are invisible to it — but any future profile change that pushes frontier
down or hops up is a measurable regression.

## 3b. Two optimizations, one loop: regret vs follow-up

The system scores both halves of the economy, and they must not be
conflated — nor may either claim more than it measures:

| | **evidence regret** | **operator follow-up** |
|---|---|---|
| optimizes | *representation*: what should cross the context boundary? | *investigation*: which evidence route to prefer, eventually |
| measures | facts the model provably used downstream | follow-up ASSOCIATION (exact-match joins), never causation |
| offline artifact | per-profile frontier gap (`ctx replay --regret`) | per-operator COUNTS (`ctx policy compile --plan-value`) |
| online consumer | digest profiles (via the regression gate) | shadow report only (`--advise` / `price --value`); promotion to a conservative tie-break waits on the paired referee |
| failure direction | one-sided: R is an upper bound, never flatters | censored never counts negative; Wilson lower bounds at read time; match classes, no confidence floats |

The known confound is stated wherever the numbers appear: follow-up rates
are entangled with when operators run in a trajectory (verifiers cluster
at the end of successes; reconnaissance at the start), so the counts feed
a report and a shadow ledger, not behavior. The promotion law: measure
associations first, demonstrate counterfactual value in shadow (paired
tasks, equal success), and only then use the ranking — as a tie-break
between actions already equivalent under hard semantics. Explicitly: no
online reinforcement learning, no runtime policy mutation, no trusted
model self-report, no automatic stopping, and hard constraints dominate
always.

## 4. Mechanism ledger: derived vs empirical

Honesty about provenance. *Derived* = the mechanism's shape follows from the
objective. *Empirical* = the mechanism was adopted because a measured A/B won
under the objective's currencies (tokens/turns/time), not because theory
demanded its exact form.

| Mechanism | Status | Note |
|---|---|---|
| Addressable store + span grammar | **derived** | the lazy-lossless constraint itself |
| Successive refinement (digest + `ctx get`) | **derived** | the two-layer code |
| Failure-asymmetric budgets | **derived** | β scales with I(Y) of failures |
| Single-boundary closure (`ctx q`) | **derived + proven** | §2 |
| Determinism / prefix stability | **derived + proven** | rate must not churn the cache prefix |
| `logtemplate/v1` (template mining) | **empirical** | a hand-built sufficient statistic for "which line is structurally rare"; validated by needle-drop 100% vs 0% |
| Head/tail evidence windows | **empirical** | encodes the prior "conclusions live at the end"; adopted after a measured miss |
| `pytest/v1..v2` census profiles | **empirical** | shape from real failure triage; sufficiency regression-gated by replay |
| Steering classifier / rewrite lanes | **empirical** | conservative by design; measured via denial/rewrite telemetry |
| Solution ladder, backward planning | **empirical** | A/B-adopted (−28% turns / −17% cost); behavioral, not information-theoretic |
| The four gates | **ontology** | a decomposition of where tokens are born, cross, stay, and leave — the map, not a theorem |

The empirical rows are not weaknesses; they are hypotheses the objective can
now score. A profile is *good* exactly insofar as its frontier → 1.0 with
hops → 0 on real trajectories — which is measurable, per profile, forever.

## 5. What would falsify, and what is next

- A profile whose frontier degrades after a change ships → the regret gate
  should have blocked it; if it didn't, the gate has a hole — fix the gate.
- A workload where R stays high with all facts inline → the digest is
  over-spending (slim it); with hops high → under-inlining (the profile is
  missing that workload's `Y`). The metric tells you *which* failure it is.
- The read-path row measures an entire channel (`read-path` frontier 0.41 on
  spec3) that today ships raw under budgets — the largest declared
  non-collapsed surface, now with a number attached.
- Oracle upgrades: SWE-Explore gold regions turn "facts used" into "facts
  needed" ([BENCHMARK.md](../evals/BENCHMARK.md)), tightening the bound.
