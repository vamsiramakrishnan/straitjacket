# Optimize evidence-plan compilation

Evolve `choose_plan(state, options) -> option_id`. Supplied evidence should not
trigger repository scans. Failures should use typed failure joins, named
symbols should use references plus focused context, and reviews should start
from the diff. Unknown work keeps a bounded total plan. Preserve evidence
coverage and verification before minimizing nodes, fanout, tokens, and time.
Change only the `EVOLVE-BLOCK`.

Each option exposes `id`, `provides`, `safe`, and the five efficiency metrics.
