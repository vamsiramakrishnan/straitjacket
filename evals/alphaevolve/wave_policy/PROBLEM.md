# Orchestration wave policy

Evolve `choose_wave(state, options) -> option_id`. Ready read-only nodes may run
in bounded parallel waves. Shared-workspace mutations must never overlap with
another node, and provider rate limits must reduce concurrency. Optimize latency,
turns, context, and dollars only after completion and workspace safety pass.
Change only the `EVOLVE-BLOCK`.

The input is a plain `dict`. Use only simple functions and control flow shown in
the seed. Reflection (`getattr`, `hasattr`), exception handling, file/network
access, private attributes, and extra dependencies are rejected by the runner.
Return exactly one option's string `id`, never the option dict or a new value.
