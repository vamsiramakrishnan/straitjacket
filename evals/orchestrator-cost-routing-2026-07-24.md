# Harness collaboration — capability × price routing (offline receipt)

**Date:** 2026-07-24 · **Mechanism:** `ctx orchestrate` (harness collaboration
orchestrator) · **Modules:** [`src/ctx/hosts.py`](../src/ctx/hosts.py),
[`src/ctx/orchestrator.py`](../src/ctx/orchestrator.py),
[`src/ctx/pricing.py`](../src/ctx/pricing.py) · **Skill:**
[`references/harness-collaboration.md`](../plugins/antigravity/skills/ctx-harness/references/harness-collaboration.md)
· **Price table:** [`model-prices.json`](../src/ctx/data/model-prices.json)

This receipt covers the **deterministic half** of the orchestrator: given the
coding-agent CLIs installed on a machine, which harness each subtask is routed
to (by capability *and* price) and what the collaboration is estimated to cost
versus running the whole task on the premium harness. It is computed offline
from the shipped price table — no CLI is launched, no tokens are billed — so it
is reproducible on any checkout. The **live billed A/B** is a declared TO-BUILD
below, exactly as the dynamic Terminal-Bench half is in
[`BENCHMARK.md`](BENCHMARK.md).

## What the orchestrator does

`ctx orchestrate "<task>"` is task coordination, not open-loop calling:

1. **Coordinate.** The cheapest installed harness — priced by its *coordinator
   model*, e.g. Antigravity on Gemini-flash-lite — reads the routing contract
   (the ctx-harness skill) and emits a `ctx.route/v1` DAG: a small set of nodes,
   each with a capability requirement (`min_tier` + tags) and dependencies. When
   no coordinator can run, a deterministic capability-routed fallback graph is
   used instead, so orchestration works offline.
2. **Assign by capability × price, at the model level.** Each node is routed to
   the *cheapest `(harness, model)` that clears its capability bar* — capability
   gates, price breaks ties (`hosts.pick_model`). Each harness runs several
   models across tiers, so exploration → an economy model, ordinary
   implementation → a cheap *standard* model (Gemini flash), planning → a
   frontier model — even within a single harness.
3. **Price & show, then run the closed loop.** The DAG is validated (acyclic,
   bounded, budgeted), priced up front, and shown before any spend. Then ready
   nodes run in parallel; each dependent sees only its upstreams' `checkpoint:`
   digests (addressed evidence, never raw bytes); a failed node escalates once
   to a stronger harness; between waves the coordinator may patch the plan with
   follow-up nodes — all bounded by `max_waves` / `max_replans` / `budget_usd`.

## Models per harness (researched 2026-07)

The unit of routing is the **model**, not the harness — each harness runs several
models across tiers. Tiers are a declared, overridable heuristic (honesty posture
of `engagement.lean_models` — the costly error is over-trusting a weak model, so
defaults fail safe).

| Harness | frontier | standard | economy | coordinator model |
|---|---|---|---|---|
| claude (Claude Code) | claude-opus-4.8 | claude-sonnet-4.6 | claude-haiku-4.5 | claude-haiku-4.5 |
| codex (Codex CLI) | gpt-5.6-sol | gpt-5.6-terra | gpt-5.6-luna | gpt-5.4-nano |
| antigravity (Gemini) | gemini-3.1-pro | gemini-3.6-flash | gemini-3.6-flash-lite | gemini-3.6-flash-lite |

## The routing: cheapest model that clears each node's tier

The default pipeline is `explore → plan → implement → verify`. Crucially,
**planning gets a frontier model; ordinary implementation gets a cheap *standard*
model** (Gemini 3.6 Flash), not a frontier one. Example plan (all three installed):

```
coordinator: antigravity [gemini-3.6-flash-lite] ~$1.00/Mout
routing (4 nodes, 4 waves):
  explore    → antigravity/gemini-3.6-flash-lite (economy→economy)  est ~$0.01
  plan       → antigravity/gemini-3.1-pro        (frontier→frontier) est ~$0.07  ⇐ explore
  implement  → antigravity/gemini-3.6-flash      (standard→standard) est ~$0.13  ⇐ plan
  verify     → codex/gpt-5.6-luna                (economy→economy)   est ~$0.04  ⇐ implement
```

`implement` lands on **Gemini 3.6 Flash** — a cheap standard model doing the
edit — while `plan` gets a frontier model. `saved` below is versus the
**single-frontier baseline**: the same token budget run entirely on the
strongest available model (here claude-opus-4.8 at $75/Mout — an aggressive but
legitimate "naive: run it all on my best model" baseline).

| Installed CLIs | est. total | single-frontier baseline | saved | implement routed to |
|---|---|---|---|---|
| claude + codex + antigravity | **$0.237** | $2.93 | **92%** | gemini-3.6-flash |
| claude + antigravity | **$0.208** | $2.93 | **93%** | gemini-3.6-flash |
| claude + codex | **$0.499** | $2.93 | **83%** | gpt-5.6-terra |
| claude **only** | **$0.816** | $2.93 | **72%** | claude-sonnet-4.6 |

Two honest readings:

- **Model routing is the lever — even within one harness.** Claude-only still
  saves 72%, because the pipeline routes explore/verify to Haiku, plan to Opus,
  and implement to Sonnet instead of running everything on Opus. You do not need
  multiple CLIs to benefit; you need multiple *models*.
- **Cheapest-model-per-tier can favor one vendor.** With this price table Gemini
  is cheapest at every tier, so the cost-only fallback routes most nodes to
  Antigravity; cross-harness happens via role coverage (verify → codex, which is
  test-oriented), a coordinator's quality pin (e.g. `"model":"claude-opus-4.8"`
  for a hard plan), escalation, or a harness being absent. The router never
  manufactures a saving the price table doesn't support.

## Reproduce

```bash
pip install -e .
python - <<'EOF'
from ctx.config import OrchestratePolicy
from ctx.hosts import detect_all
from ctx.orchestrator import fallback_route, render_route_plan

def which(*names):          # simulate an install set, no real CLI needed
    s = set(names)
    return lambda b: f"/usr/bin/{b}" if b in s else None

for combo in [("claude","codex","antigravity"), ("claude","codex"), ("claude",)]:
    hosts = [d for d in detect_all(which=which(*combo)) if d.installed and d.harnessable]
    print(render_route_plan(fallback_route("example task", hosts, OrchestratePolicy())), "\n")
EOF
```

On a real machine, `ctx wrap detect` prices whatever CLIs are on PATH, and
`ctx orchestrate "<task>" --dry-run` prints the priced route for that exact set.

## Determinism & bounds

Capability routing sorts by `(-tags_covered, output price, input price, name)`
and the coordinator ladder by coordinator-model price, so the fallback route and
the priced plan are byte-identical for a fixed install set and price table —
asserted in [`tests/test_orchestrator.py`](../tests/test_orchestrator.py) and
[`tests/test_hosts.py`](../tests/test_hosts.py). The closed loop is bounded by
`max_nodes` / `max_waves` / `max_replans` / `budget_usd` in `ctx.toml
[orchestrate]`; every step is fail-open. Prices are estimates; a session's real
spend is read from wire truth (`ctx.scorecard`) after each node runs.

## TO-BUILD: the live billed A/B

Not yet measured: a real task driven end-to-end across two live harnesses,
comparing **billed tokens** for the coordinated route (cheap-explore →
frontier-implement → cheap-verify, handoff via checkpoint, coordinator on
flash-lite) against a single-frontier-harness run of the same task. The harness
is the same shape as [`ab_eval_live.py`](ab_eval_live.py); the blocker is
identical to the Antigravity receipt's — headless, API-key-driveable access to
two hosts at once. Recorded as debt rather than asserted as a result.
