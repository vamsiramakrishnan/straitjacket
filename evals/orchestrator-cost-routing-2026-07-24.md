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
2. **Assign by capability × price.** Each node is routed to the *cheapest
   installed harness that clears its capability bar* — capability gates, price
   breaks ties (`hosts.pick_worker`). Economy work (search/triage/verify) lands
   on the economy harness; frontier work (synthesis/edit/decide) on the frontier
   harness.
3. **Price & show, then run the closed loop.** The DAG is validated (acyclic,
   bounded, budgeted), priced up front, and shown before any spend. Then ready
   nodes run in parallel; each dependent sees only its upstreams' `checkpoint:`
   digests (addressed evidence, never raw bytes); a failed node escalates once
   to a stronger harness; between waves the coordinator may patch the plan with
   follow-up nodes — all bounded by `max_waves` / `max_replans` / `budget_usd`.

## Capability tiers

Declared, overridable heuristic (honesty posture of `engagement.lean_models` —
the costly error is over-trusting a weak model, so defaults fail safe):

| Host | tier | strengths | coordinator model |
|---|---|---|---|
| antigravity | economy | search, triage, bulk, verify, summarize, explore | gemini-3.5-flash-lite |
| codex | standard | code, implement, edit, test | gpt-5.4-nano |
| claude | frontier | reason, synthesize, implement, edit, code, review, decide | claude-haiku |

## The one variable: which harnesses are installed

Estimates use the default per-node token budgets and the shipped list prices;
`saved` is versus the **single-premium baseline** — the same token budget run
entirely on the premium harness. Example plan (all three installed):

```
coordinator: antigravity [gemini-3.5-flash-lite] ~$1.20/Mout
routing (3 nodes, 3 waves):
  wave 1:  explore    → antigravity (economy/economy)   est ~$0.02
  wave 2:  implement  → claude      (frontier/frontier)  est ~$0.28  ⇐ explore
  wave 3:  verify     → antigravity (economy/economy)    est ~$0.02  ⇐ implement
estimated total: ~$0.32  (single-premium baseline ~$0.49, saves ~$0.18)
```

| Installed CLIs | est. total | single-premium baseline | saved |
|---|---|---|---|
| claude + codex + antigravity | **$0.3175** | $0.4935 | **$0.176 (36%)** |
| claude + antigravity | **$0.3175** | $0.4935 | **$0.176 (36%)** |
| claude + codex | **$0.4715** | $0.4935 | $0.022 (4%) |
| claude only | $0.4935 | $0.4935 | $0.000 (0%) |

Two honest readings:

- **The economy harness is where the savings live.** With an economy-tier CLI
  present, exploration and verification move off the frontier model and the plan
  is ~36% cheaper.
- **Collaboration is not free money.** Two standard-tier harnesses spread only
  ~4%, and a single harness degrades to that harness with `est. total ==
  baseline` — zero claimed saving. The router never manufactures a saving the
  price table doesn't support.

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
