# Checkpoint-handoff policy

Evolve `choose_handoff(state, options) -> option_id`. Every handoff retains an
exact address. Dependents need a bounded summary, mutations and verification
need decision evidence, and failures need diagnostic detail. Terminal successes
may carry only the address. Minimize repeated context after these gates pass.
Change only the `EVOLVE-BLOCK`.

The input is a plain `dict`. Use only simple functions and control flow shown in
the seed. Reflection (`getattr`, `hasattr`), exception handling, file/network
access, private attributes, and extra dependencies are rejected by the runner.
Return exactly one option's string `id`, never the option dict or a new value.
