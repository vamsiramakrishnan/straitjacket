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
versus single-model baselines — honestly, against Opus *and* Sonnet, since the
comparison flatters or flops depending on which you pick. It is computed offline
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
| antigravity (Gemini) | gemini-3.1-pro | gemini-3.6-flash | gemini-3.5-flash-lite | gemini-3.5-flash-lite |

## The routing: the flagship plans, the cheap model implements

The default pipeline is `explore → plan → implement → verify`, and each role
routes to a deliberately different model:

- **plan → the frontier *flagship* (Opus)** via `prefer:"strong"` — a good plan
  is worth the strong model, so planning does *not* take the cheapest frontier
  model.
- **implement → complexity-adaptive:** `standard` (Gemini 3.6 Flash) for real
  work, `economy` (Gemini 3.5 Flash-lite) for a simple edit
  (`[orchestrate] implement_tier`, or the coordinator's judgment per task).
- **explore / verify → economy** (Gemini flash-lite).

Example plan (all three installed, complex implement):

```
routing (4 nodes, 4 waves):
  explore    → antigravity/gemini-3.5-flash-lite (economy)   est ~$0.01
  plan       → claude/claude-opus-4.8            (frontier)  est ~$0.47  ⇐ explore   [prefer strong]
  implement  → antigravity/gemini-3.6-flash      (standard)  est ~$0.13  ⇐ plan
  verify     → antigravity/gemini-3.5-flash-lite (economy)   est ~$0.01  ⇐ implement
```

A *simple* task instead routes `implement → antigravity/gemini-3.5-flash-lite`
(economy). Routed total is **~$0.61** for the same 108k-in/17.5k-out budget. What
that costs against a *single-model* baseline depends entirely on which model you
compare to — pick the baseline honestly:

| Single-model baseline (whole task on one model) | its cost | routing vs it |
|---|---|---|
| all-Opus (your most expensive model) | $2.93 | **−79%** (routing far cheaper) |
| **all-Sonnet** (a realistic coding default) | $0.59 | **+4% — routing is slightly *more* expensive** |
| all-Haiku (cheapest, low quality) | $0.20 | +211% (routing much dearer) |

**Read this straight: the headline "72–79% cheaper" is only true against
all-Opus, which nobody actually runs.** Against a sane default (all-Sonnet)
routing is roughly break-even — because the ~$0.47 spent on the Opus *plan* node
cancels what you save routing the bulk to Gemini flash / flash-lite. So the
mechanism is not a dollar-saver against a reasonable baseline; it is a **quality
allocator**: it spends flagship money *only on planning* and keeps every other
phase cheap, for about the price of a flat Sonnet run. If you don't want the Opus
plan, set `prefer:cheap` on it and the total drops below Sonnet.

Two honest readings:

- **It's allocation, not savings.** Model routing lets you buy Opus-grade
  planning without paying Opus rates for explore/implement/verify — net cost
  ≈ a flat Sonnet run, with a stronger plan and cheaper bulk. The only baseline
  it beats by a lot is the one where you'd have run *everything* on your top
  model.
- **Cheapest-model-per-tier can favor one vendor.** With this price table Gemini
  is cheapest at economy/standard, so implement and explore/verify land on
  Antigravity while plan lands on Claude's Opus. Cross-harness beyond that
  happens via role coverage, a coordinator pin, escalation, or a harness being
  absent. The router never manufactures a saving the price table doesn't
  support.

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

## Live: the collaboration runs (proven), the full A/B still TO-BUILD

The two-model collaboration is now demonstrated live — real Gemini (Antigravity's
model) + real Claude through the actual `run_route` loop, with the CAS checkpoint
handoff verified and real tokens billed:
[`live-collab-antigravity-claude-2026-07-24.md`](live-collab-antigravity-claude-2026-07-24.md).

Still **TO-BUILD**: a full A/B comparing billed tokens for a coordinated route
against a single-model baseline on a hard task (the numbers above are the
deterministic estimate, not a live A/B). The remaining blocker is the same as
the Antigravity receipt's — headless access to enough hosts to run both arms;
the live receipt drives Antigravity's model via the API (its CLI is OAuth-only)
and does not exercise Codex.
