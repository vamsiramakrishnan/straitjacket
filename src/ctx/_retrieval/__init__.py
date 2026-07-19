"""Private implementation package backing ``ctx.retrieval`` (SPEC §6.3-6.5).

Nothing here is a stable import path — everything importable is re-exported
by ``ctx.retrieval``, which is the byte-compatible facade every other module
(``cli``, ``mcp``, ``codeverbs``, ``rundiff``, ``digest``, tests, evals)
imports from. Submodules exist purely to keep each concern (targets, the
optional ripgrep engine, search, get, spans, stats, telemetry) small enough
to read in one sitting; import from ``ctx.retrieval`` instead of reaching
into these directly.
"""
