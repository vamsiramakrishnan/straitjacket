# Harness collaboration — cost-routing model (offline receipt)

**Date:** 2026-07-24 · **Mechanism:** `ctx orchestrate` (harness collaboration
orchestrator) · **Modules:** [`src/ctx/hosts.py`](../src/ctx/hosts.py),
[`src/ctx/orchestrator.py`](../src/ctx/orchestrator.py),
[`src/ctx/pricing.py`](../src/ctx/pricing.py) · **Price table:**
[`src/ctx/data/model-prices.json`](../src/ctx/data/model-prices.json)

This receipt covers the **deterministic half** of the orchestrator: given the
coding-agent CLIs installed on a machine, which harness each phase of a task is
routed to and what the collaboration is estimated to cost versus running every
phase on the premium harness. It is computed offline from the shipped price
table — no CLI is launched, no tokens are billed — so it is reproducible on any
checkout. The **live billed A/B** (real multi-harness token spend on a real
task) is a declared TO-BUILD below, exactly as the dynamic Terminal-Bench half
is in [`BENCHMARK.md`](BENCHMARK.md).

## What the orchestrator does

`ctx orchestrate "<task>"` detects the installed, harnessable CLIs
(`ctx wrap detect`), ranks them cheapest→premium by their model's list price
(`ctx.pricing`), and assigns the default three-phase pipeline by cost:

| Phase | Role | Routed to | Why |
|---|---|---|---|
| explore | lean | cheapest installed harness | search/read/map — gather evidence into the CAS; a cheap model over-executes affordances anyway (see `ctx.engagement`) |
| implement | capable | premium installed harness | synthesis/edit from the addressed evidence — the phase that needs the strong model |
| review | lean | cheapest installed harness | run the acceptance check, inspect the diff |

The handoff between phases is a `checkpoint:` in the shared artifact store: each
phase deposits its output as a `blob:` and freezes a checkpoint citing it; the
next phase's prompt carries only that bounded checkpoint, never the raw
exploration bytes. This is the cross-*harness* generalization of the shipped
`ctx-explorer` sub-agent quarantine (ROADMAP M-A).

## The one variable: which harnesses are installed

Estimates use the default per-phase token budgets from the `[orchestrate]`
config block (explore 24k/3k, implement 48k/9k, review 20k/2.5k in/out) and the
shipped list prices. `saved` is versus the **single-premium baseline** — the
same token budget run entirely on the premium harness.

| Installed CLIs | explore | implement | review | est. total | single-premium baseline | saved |
|---|---|---|---|---|---|---|
| claude + codex + antigravity | antigravity | claude | antigravity | **$0.3175** | $0.4935 | **$0.176 (36%)** |
| claude + antigravity | antigravity | claude | antigravity | **$0.3175** | $0.4935 | **$0.176 (36%)** |
| claude + codex | codex | claude | codex | **$0.4715** | $0.4935 | $0.022 (4%) |
| claude only | claude | claude | claude | $0.4935 | $0.4935 | $0.000 (0%) |

Two honest readings fall straight out of the table:

- **The economy harness is where the savings live.** When an economy-tier CLI
  is present (Antigravity's Gemini-flash default at $0.5/$3 per 1M in/out), the
  two lean phases move off the premium model and the plan is ~36% cheaper.
- **Collaboration is not free money.** With only two standard-tier harnesses
  (claude + codex, both ~$15/1M out) the spread is small (4%), and with a
  single harness the orchestrator degrades honestly to that harness with zero
  claimed savings — `est. total == baseline`. The router never manufactures a
  saving that the price table doesn't support.

## Reproduce

```bash
pip install -e .
python - <<'EOF'
from ctx.config import OrchestratePolicy
from ctx.hosts import detect_all
from ctx.orchestrator import plan_orchestration, render_plan

def which(*names):          # simulate an install set, no real CLI needed
    s = set(names)
    return lambda b: f"/usr/bin/{b}" if b in s else None

for combo in [("claude","codex","antigravity"), ("claude","codex"), ("claude",)]:
    hosts = [d for d in detect_all(which=which(*combo)) if d.installed and d.harnessable]
    plan = plan_orchestration("example task", hosts, OrchestratePolicy())
    print(render_plan(plan), "\n")
EOF
```

On a real machine, `ctx wrap detect` prices whatever CLIs are actually on PATH,
and `ctx orchestrate "<task>" --dry-run` prints the priced plan for that exact
install set.

## Determinism

The cost ladder sorts by `(output price, input price, name)`, so the routing and
the printed plan are byte-identical for a fixed install set and price table —
asserted in [`tests/test_orchestrator.py`](../tests/test_orchestrator.py)
(`test_render_plan_is_deterministic`, `test_cost_ladder_cheapest_first`). Prices
are estimates; a session's real spend is still read from wire truth
(`ctx.scorecard`) after each phase runs.

## TO-BUILD: the live billed A/B

What this receipt does **not** yet measure: a real task driven end-to-end across
two live harnesses, comparing **billed tokens** for the collaboration
(cheap-explore → premium-implement → cheap-review, handoff via checkpoint)
against a single-premium-harness run of the same task. The harness for it is the
same shape as [`ab_eval_live.py`](ab_eval_live.py): fix the fixture task, drive
arm A (single premium harness) and arm B (`ctx orchestrate`), and diff
provider-reported usage from each host's wire tap. The blocker is identical to
the Antigravity receipt's: headless, API-key-driveable access to two hosts at
once. Recorded here as debt rather than asserted as a result.
