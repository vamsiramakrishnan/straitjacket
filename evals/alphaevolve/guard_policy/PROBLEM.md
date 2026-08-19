# Optimize the birth gate without weakening safety

Evolve `choose_guard(state, options) -> option_id`. Secrets and outside-root
reads require a visible permission step. Explicit denies and destructive
commands remain blocked. Large reads and known floods should be rewritten to
bounded equivalents while bounded/structured operations pass through. Known
read-only noisy commands should execute through containment without a manual
retry. Unknown commands retain a visible permission boundary. Safety failures
are terminal regardless of cost. Change only the `EVOLVE-BLOCK`.

Each option exposes `id`, `provides`, `safe`, and the five efficiency metrics;
the evaluator independently enforces the case's admissible safety outcome.
