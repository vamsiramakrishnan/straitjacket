# Harness collaboration — routing a task across harnesses by capability × price

`ctx orchestrate "<task>"` runs a task as a *collaboration* between the coding-agent
CLIs installed on the machine, coordinated by the cheapest one. This reference is
the contract the coordinator follows; it is kept in lockstep with
`ROUTING_CONTRACT` in `ctx/orchestrator.py` so the coordinator behaves the same
whether it read this skill or only the inlined prompt.

## The idea

Not every part of a task needs the strongest model. Searching, triaging logs,
and running a verification are cheap work; synthesis and code edits are not.
The coordinator (a cheap model — e.g. Antigravity on Gemini-flash-lite) splits
the task into a small dependency graph and assigns each node to the harness
whose *capability* fits, spending the *cheapest* harness that clears the bar.
Nodes hand off **addressed evidence** — a `checkpoint:` in the shared store —
never raw bytes, so a dependent sees a bounded digest and resolves handles with
`ctx get`.

This is task coordination, not open-loop calling: independent nodes run in
parallel; a failed node escalates to a stronger harness; after a wave the
coordinator may add follow-up nodes from what came back.

## The menu

`ctx orchestrate` hands the coordinator the installed harnesses with their
capability tier, list price, and strengths, e.g.:

```
harnesses available (capability · $in/$out per 1M):
  antigravity economy   $0.50/$3.00   strengths: search, triage, bulk, verify, summarize, explore
  codex       standard  $2.50/$15.00  strengths: code, implement, edit, test
  claude      frontier  $3.00/$15.00  strengths: reason, synthesize, implement, edit, code, review, decide
```

Capability tiers, strongest → weakest: **frontier > standard > economy**. A
node's `min_tier` is the *weakest* tier that can do it; the router picks the
cheapest installed harness at or above that tier that covers the node's
capability tags.

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

Per-node fields: `id` (unique), `goal` (what the assigned harness must do),
`role` (a short label), `min_tier`, `needs` (capability tags), `deps` (node ids
that must finish first — their checkpoints are handed to this node),
`est_input_tokens` / `est_output_tokens` (to price the plan). Optional
`"host": "<name>"` pins a specific harness instead of letting the router choose.

## Rules

1. **Decompose only where it helps.** A trivial task is ONE node. Fan out only
   when subtasks are genuinely independent (they run in parallel) or form a real
   dependency chain.
2. **Cheapest tier that can do the work.** Put exploration / search / triage /
   verification at `economy`; code generation at `standard`; synthesis, edits,
   and decisions at `frontier`. Do not send everything to the frontier model —
   that defeats the point.
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
