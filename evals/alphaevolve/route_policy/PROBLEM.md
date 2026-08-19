# Minimize success-adjusted model routing cost

Evolve `choose_route(task, routes)` to select one route that completes the task.
Tasks describe complexity, risk, context need, latency sensitivity, and whether
independent review is required. Routes describe capability, context window,
planning/review coverage, model-visible tokens, expected repair turns, dollars,
and latency.

Completion is a hard gate: never save money by choosing a route that lacks the
capability, context, planning, or review needed for the task. Among policies
that complete every hidden task, prefer lower total dollars, fewer visible
tokens, fewer repair turns, and then lower latency. Return an existing route ID.
The evaluator is deterministic and does not launch any agent. Change only the
single `EVOLVE-BLOCK`.
