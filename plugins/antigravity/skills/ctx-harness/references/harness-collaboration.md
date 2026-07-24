# Harness collaboration — routing a task across harnesses by capability × price

`ctx orchestrate "<task>"` runs a task as a *collaboration* between the coding-agent
CLIs installed on the machine, coordinated by the cheapest one. This reference is
the contract the coordinator follows; it is kept in lockstep with
`ROUTING_CONTRACT` in `ctx/orchestrator.py` so the coordinator behaves the same
whether it read this skill or only the inlined prompt.

## The idea

Not every part of a task needs the strongest model, and the unit of routing is
the **model**, not the harness — each harness runs several models across tiers.
Searching and triaging are economy work; **ordinary implementation and edits are
fine on a cheap *standard* model** (e.g. Gemini 3.6 Flash), not a frontier one;
architecture and hard reasoning want a frontier model (Opus, Gemini 3.1 Pro, GPT
Sol). The coordinator (a cheap model — Antigravity on Gemini-flash-lite) splits
the task into a small dependency graph and assigns each node a `(harness, model)`
by *capability × price*: the cheapest model that clears the node's tier and
covers its roles. Nodes hand off **addressed evidence** — a `checkpoint:` in the
shared store — never raw bytes.

This is task coordination, not open-loop calling: independent nodes run in
parallel; a failed node escalates to a stronger model; after a wave the
coordinator may add follow-up nodes from what came back.

## Supported models per harness (2026-07)

Researched from Claude Code `/model`, the Codex model picker, and Antigravity's
model list. Tiers are a declared, overridable heuristic. Adding a model is a
data edit in `ctx/hosts.py` (`HostSpec.models`); prices live in
`ctx/data/model-prices.json`.

| Harness | frontier (plan/reason) | standard (implement/edit) | economy (explore/verify) |
|---|---|---|---|
| **claude** (Claude Code) | claude-opus-4.8 | claude-sonnet-4.6 | claude-haiku-4.5 |
| **codex** (Codex CLI) | gpt-5.6-sol | gpt-5.6-terra | gpt-5.6-luna |
| **antigravity** (Gemini) | gemini-3.1-pro | gemini-3.6-flash | gemini-3.5-flash-lite |

(Antigravity is BYO-model and can also run Claude/GPT; only its Gemini tiers are
modeled here.) `ctx orchestrate` passes the *installed* subset of this catalog to
the coordinator as the live menu, with each model's list price.

## The menu

`ctx orchestrate` hands the coordinator every installed `(harness, model)` with
tier, price, and roles, e.g.:

```
harnesses & models available (model · tier · $in/$out per 1M · roles):
  antigravity:
    gemini-3.1-pro         frontier  $2.00/$12.00  plan, reason, review, architect
    gemini-3.6-flash       standard  $1.25/$7.50   implement, edit, code, summarize
    gemini-3.5-flash-lite  economy   $0.15/$1.00   explore, search, triage, verify
  claude:
    claude-opus-4.8        frontier  $15.00/$75.00 plan, reason, synthesize, decide, review, architect
    claude-sonnet-4.6      standard  $3.00/$15.00  implement, edit, code, review
    claude-haiku-4.5       economy   $1.00/$5.00   explore, search, triage, verify, summarize
```

Capability tiers, strongest → weakest: **frontier > standard > economy**. A
node's `min_tier` is the *weakest* tier that can do it; the router picks the
cheapest `(harness, model)` at or above that tier that covers the node's roles.
Pin `"model"` on a node to override — e.g. planning is often worth Opus even
when a cheaper frontier model exists.

## The output contract — `ctx.route/v1`

Output **only** a JSON object, no prose:

```json
{
  "schema": "ctx.route/v1",
  "nodes": [
    {"id": "scan", "goal": "find the retry sites and the test entry point",
     "role": "search", "min_tier": "economy", "needs": ["search","triage"],
     "deps": [], "est_input_tokens": 15000, "est_output_tokens": 2000},
    {"id": "impl", "goal": "add the retry from the scan checkpoint",
     "role": "implement", "min_tier": "frontier", "needs": ["synthesize","edit"],
     "deps": ["scan"], "est_input_tokens": 40000, "est_output_tokens": 8000},
    {"id": "verify", "goal": "run the tests and inspect the diff",
     "role": "verify", "min_tier": "economy", "needs": ["test","verify"],
     "deps": ["impl"], "est_input_tokens": 10000, "est_output_tokens": 1200}
  ]
}
```

Per-node fields: `id` (unique), `goal` (what the assigned model must do),
`role` (a short label), `min_tier`, `needs` (capability tags), `deps` (node ids
that must finish first — their checkpoints are handed to this node),
`est_input_tokens` / `est_output_tokens` (to price the plan). Optional
`"host": "<name>"` and/or `"model": "<id from the menu>"` pin a specific harness
or model; `"prefer": "strong"` takes the flagship at the tier (Opus for a
frontier plan) instead of the cheapest eligible model.

## Rules

1. **Decompose only where it helps.** A trivial task is ONE node. Fan out only
   when subtasks are genuinely independent (they run in parallel) or form a real
   dependency chain.
2. **Route by model, and judge complexity.**
   - Exploration / search / triage / verification → `economy`.
   - **Implementation is complexity-adaptive:** a SIMPLE edit (a line, a small
     well-specified function) → `economy` (the cheapest model, Gemini
     3.5-flash-lite); a COMPLEX change (multiple files, real design, tricky
     logic) → `standard` (Gemini 3.6-flash). Judge the task; don't default
     everything to one tier.
   - **Planning / architecture / hard reasoning → `frontier` with
     `"prefer": "strong"`**, so it takes the flagship (Opus), not the cheapest
     frontier model — a good plan is worth the strong model.
   Pin `"model"` only when a specific model matters beyond this.
3. **Keep the graph acyclic and small** (bounded by `[orchestrate] max_nodes`).
   `deps` express ordering; a dependent waits for its upstreams and receives
   their checkpoints.
4. **Re-planning is bounded.** If a node fails and the runner asks for a patch,
   emit `ctx.route/v1` with ONLY the follow-up nodes to recover — new ids, deps
   may reference completed nodes. This is capped by `max_replans`.

## Bounds and fail-open

The loop stops at the first of `max_waves`, `max_replans`, or `budget_usd`
(in `ctx.toml [orchestrate]`). If no coordinator can run, `ctx orchestrate`
falls back to a deterministic capability-routed graph (explore → implement →
verify). A single installed harness degrades to that harness with no claimed
saving. Every step is fail-open — a missing or failing harness is recorded and
skipped, never fatal.
