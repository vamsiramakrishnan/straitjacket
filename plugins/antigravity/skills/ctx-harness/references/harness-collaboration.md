# Delegate a task with `ctx orchestrate`

When coordinating, return a `ctx.route/v1` graph. Give each node a goal,
role, minimum capability tier, capability tags, dependencies and estimated
token counts. A simple question can use one node. Split independent questions
when workers can answer them concurrently. A node that edits code requires a
separate downstream verification node.

```json
{
  "schema": "ctx.route/v1",
  "nodes": [
    {
      "id": "inspect",
      "goal": "Find the timeout path and cite its tests",
      "role": "explore",
      "min_tier": "economy",
      "needs": ["search", "triage"],
      "deps": [],
      "est_input_tokens": 15000,
      "est_output_tokens": 2000
    },
    {
      "id": "implement",
      "goal": "Update the timeout path described in the inspect checkpoint",
      "role": "implement",
      "min_tier": "standard",
      "needs": ["implement", "edit", "code"],
      "deps": ["inspect"],
      "est_input_tokens": 30000,
      "est_output_tokens": 5000
    },
    {
      "id": "verify",
      "goal": "Run the named timeout tests and inspect the diff",
      "role": "verify",
      "min_tier": "economy",
      "needs": ["test", "verify"],
      "deps": ["implement"],
      "est_input_tokens": 10000,
      "est_output_tokens": 1200
    }
  ]
}
```

## Select workers

- Choose candidates from the host and model menu in the coordinator prompt.
  Installations and ACP endpoints vary by workspace. An ACP endpoint offers
  its configured model. Every worker launch checks that endpoint's model ID.
- Request `economy` for a focused search, triage or a named test. Choose
  `standard` for an edit with several interacting parts. Reserve `frontier`
  for architecture or hard reasoning. A frontier planning node can set
  `"prefer": "strong"` to favor the highest tier and flagship price among
  workers with matching role coverage.
- Use `needs` for capability tags. A worker's own harness controls local skill
  loading. If the job depends on a particular installed skill, name it in
  `goal` and select a host that provides it.
- To request an exact pair, supply both `host` and `model` from the menu.
  The router can fall back when a pin is unavailable. Operators can inspect
  the assignment with `ctx orchestrate 'task' --dry-run`. The preview uses
  local routing and makes no model calls. See `references/model-catalog.md`
  for the interpretation of model measurements.
- If no candidate clears the requested tier, the preview flags **tier unmet**.
  A node can declare repository-relative `targets`. Limit concurrent mutation
  to independent nodes with disjoint declared targets and worktree isolation
  enabled. A node may request a JSON `output_schema` and set
  `strict_output_schema` for enforcement.

## Hand work to the next node

Each dependent worker receives a bounded handoff with `checkpoint:` and
evidence addresses. Cite the relevant address and location in the finding.
The next worker can use `ctx get` to read the evidence in the shared workspace.
For a finding outside the dependency chain, send an address to a task node
with `ctx task send`, or to a harness using `ctx relay signal`.

If the orchestrator asks for a replan, return only the additional nodes.
Give each a new ID. It may depend on a completed node and reuse that node's
checkpoint. The task ledger records attempts, budget reservations and recovery.
