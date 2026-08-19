# Optimize retrieval

Evolve `choose_retrieval(state, options) -> option_id`. Use exact addresses
when present, typed failure facts before raw searching, symbol references for
named code, and bounded maps for unknown scope. Required context must arrive;
dry or repeated broad reads are not completion. Minimize visible tokens, tool
calls, and turns. Change only the `EVOLVE-BLOCK`.

Each option exposes `id`, `provides`, `safe`, and the five efficiency metrics.
