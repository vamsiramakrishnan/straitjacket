# Improve generic evidence selection

Evolve `select_evidence(lines, budget)` so a bounded straitjacket digest keeps
the lines most useful for diagnosing or verifying a command result.

The returned value must be a deterministic, ascending list of unique,
zero-based indices. It must never exceed `budget`, contain an invalid index, use
I/O or network access, mutate its inputs, or depend on randomness, clocks,
process state, external data, or third-party packages. Small pure-stdlib
imports may be used when needed; unsafe or unallowlisted imports are rejected. Higher
`evidence_utility` is better. Invalid, non-deterministic, over-budget, or slow
candidates receive a large negative score.

Useful evidence often includes the root failure, its source coordinate, the
terminal summary, a request or operation identifier, and enough beginning/end
context to establish what ran. Avoid being fooled by benign phrases such as
"no errors detected". The evaluator uses frozen command-output families that
are not embedded in this prompt.

Change only the single `EVOLVE-BLOCK`. Keep the public function name and
signature. Prefer small, legible, linear-time heuristics suitable for a CLI hot
path.
