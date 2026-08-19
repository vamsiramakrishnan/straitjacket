# Verification-route policy

Evolve `choose_verification(state, options) -> option_id`. All mutations remain
verified. High-risk work requires review-capable verification and uses an
independent host when available. Complex mutations prefer independent checking;
small, low-risk work should avoid redundant cross-host cost. Change only the
`EVOLVE-BLOCK`.

The input is a plain `dict`. Use only simple functions and control flow shown in
the seed. Reflection (`getattr`, `hasattr`), exception handling, file/network
access, private attributes, and extra dependencies are rejected by the runner.
Return exactly one option's string `id`, never the option dict or a new value.
