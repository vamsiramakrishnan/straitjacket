# Context efficiency implementation and release gates

The work starts at `ba61748`, the LSP edit-loop change merged through PR #48.
The objective is fewer discovery and repair operations with retained evidence
and passing task checks. Lower tool-output counts alone do not establish lower
provider cost or better task completion.

## Work sequence

1. Verify the existing LSP edit loop with installed TypeScript, Go and Rust
   servers. Exercise cross-file rename, source freshness and executable checks.
   Record exact server versions and distinguish unavailable diagnostics from
   clean reports. Release only after the package and regression gates pass.
2. Extend the existing context pack with bounded snippets and relationships.
   Keep multi-file edits on the sealed transaction path and retain diagnostics
   and behavioral verification receipts.
3. Partition reusable indexes by branch identity while validating working-tree
   changes. Check branch switching, dirty files and separate worktrees.
4. Account for model input, cache reads/writes, output and tool operations
   separately. Missing provider usage and rates remain unknown. Never score a
   failed task as an efficiency improvement.
5. Compare pinned tools on identical inputs and acceptance checks. Run local
   CPU-only operations here; record unavailable or authenticated integrations
   explicitly. A scripted tool replay is not a live-agent trial.

## Verification and delivery

Each workstream has an isolated checkout and commits independently. Integration
requires inspecting the changes and receipts, focused tests, full and builtin
engine suites, and installed-wheel validation. CI must pass before merge and
release publication. The release record must distinguish implemented features,
local experiments and unmeasured provider economics.

Decisions and their evidence are recorded in
[`improvements-2026-10-29.tsv`](improvements-2026-10-29.tsv).
