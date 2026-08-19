# Optimize execution, backgrounding, and cache reuse

Evolve `choose_execution(state, options) -> option_id`. Reuse only an exactly
valid content-addressed cache. Stale or uncertain materializations rebuild.
Long processes background without polling, floods remain captured, and tiny
commands may stay inline. Completion, freshness, and addressability are hard
gates. Change only the `EVOLVE-BLOCK`.

Each option exposes `id`, `provides`, `safe`, and the five efficiency metrics;
cache freshness is an evaluator-owned hard constraint.
