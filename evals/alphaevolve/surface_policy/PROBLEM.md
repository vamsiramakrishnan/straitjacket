# Minimize the capability surface

Evolve `choose_surface(state, options) -> option_id`. Keep every capability
required to complete the task and never weaken the authority ceiling. Minimize
static model-visible tokens first because they are resent on every turn. Keep
unknown and high-risk work on the full reviewed surface. Change only the
`EVOLVE-BLOCK`.

Each option exposes `id`, `provides`, `safe`, `dollars`, `visible_tokens`,
`model_turns`, `tool_calls`, and `latency_ms`.
