# Choose the executing harness for one bounded task

Evolve `choose_harness(task, options)` so straitjacket routes each delegated
task to the right coding harness (codex, claude, gemini, agy) or keeps it on
the local coordinator.

The returned value must be exactly one string option id from `options`. A
choice is admissible only when the case's `allowed` set contains it, and it is
complete only when the selected option provides every required capability
(mutation capability for edits, independent review for cross-checks,
unattended capability for headless runs, and so on). Explicit user host pins
are authoritative. Unknown task shapes fall back to the coordinator instead of
guessing. Material marked as secret must stay on the coordinator and never be
handed to an external harness.

No I/O, no randomness, no clocks, no third-party packages; pure deterministic
stdlib logic only. Invalid, unsafe, inadmissible, or incomplete candidates
receive a large negative score. Among complete choices, lower dollars, fewer
visible tokens, fewer turns, fewer tool calls, and lower latency win — the
shared multiplicative metric handles that part.

Change only the single `EVOLVE-BLOCK`. Keep the public function name and
signature. Prefer a small, legible decision ladder suitable for a hot path.
