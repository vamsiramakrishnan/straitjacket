# Mutation-isolation policy

Evolve `choose_mutation_isolation(state, options) -> option_id`. Parallel mutation
is admissible only when every mutation has its own worktree, write targets are
declared, and targets are disjoint. Shared-workspace writes must serialize.
Change only the `EVOLVE-BLOCK`.

The input is a plain `dict`. Use only simple functions and control flow shown in
the seed. Reflection (`getattr`, `hasattr`), exception handling, file/network
access, private attributes, and extra dependencies are rejected by the runner.
Return exactly one option's string `id`, never the option dict or a new value.
