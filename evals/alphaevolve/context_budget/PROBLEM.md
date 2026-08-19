# Allocate context without losing task-critical evidence

Evolve `allocate_context(items, budget_tokens)` to choose the smallest useful
set of evidence items that lets a coding agent complete its task. Return unique,
ascending, zero-based indices whose summed `tokens` never exceed the budget.

Items expose evidence kind, severity, novelty, addressability, token cost, and
position. Root causes, failure identities, verification results, source
coordinates, terminal summaries, and retrieval addresses are generally more
valuable than repeated context, teaching prose, or noise. Do not assume every
budget should be filled.

The hidden evaluator hard-gates mandatory evidence and task completion, then
rewards retained utility and fewer model-visible tokens. The function must be
deterministic, side-effect free, and approximately linearithmic or better.
Change only the single `EVOLVE-BLOCK`.
