# Routing policy

Use CTX when output cardinality is unbounded, unknown, or likely to exceed the configured inline budget.

## Always route

- tests, builds, compilers, linters, type-checkers, benchmarks;
- logs, traces, recursive directory trees, broad searches;
- large Git diff/log output;
- cloud, container, database, and API commands returning arrays/pages;
- files larger than the inline byte threshold;
- JSON/JSONL/CSV where record count is unknown.

## Usually safe natively

- a known small file;
- an exact line slice under budget;
- a bounded status command;
- a command whose output is redirected and whose console response is proven small.

Unknown shell expressions should use `ctx run --shell -- '<command>'` only after the normal Antigravity permission step.
