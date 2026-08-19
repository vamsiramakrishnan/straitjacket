# Beat the naive fast-path baseline

Evolve `choose_fast_path(task, plans)` for ordinary, bounded assistant tasks.
Return exactly one plan ID supplied in `plans`.

The naive seed sends every request through `broad_standard`: it completes the
task, but reads too much, spends too many tokens, and takes extra model/tool
turns. Discover narrower paths from signals such as `provided_context`,
`target_known`, `changes_present`, `failure_present`, and `mutation`.

Use each plan's `capabilities` field; do not infer safety from its name alone.
The ordinary completion contract is:

- supplied context that needs only explanation or diagnosis requires `answer`;
- explanation or inspection of a known target requires `read` and `answer`;
- a known-target mutation requires `read`, `edit`, and `verify`;
- a named test request requires `test`;
- review of existing changes requires `diff` and `answer`;
- an unknown-target mutation with a failure requires `search`, `read`, `edit`,
  and `verify`.

Missing supplied context is not by itself a reason to use the broad path: a
known target can be retrieved with a focused read. Prefer the lowest-cost plan
whose capabilities cover the task, with `broad_standard` as the safe fallback.

Completion is a hard gate. A cheap no-op, an answer without required evidence,
an unverified edit, or a plan that cannot locate an unknown target receives a
large penalty. Among policies that complete every task, maximize the Pareto
improvement over `broad_standard` in visible tokens, model turns, tool calls,
and estimated dollars. Do not add network, filesystem, subprocess, randomness,
clock, or environment access.
