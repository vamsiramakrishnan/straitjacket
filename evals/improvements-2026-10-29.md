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

## Results

- LSP: [real server receipts](lsp-languages-2026-10-29.md) verify TypeScript,
  Go and Rust edits, stale-plan refusal and executable behavior. Go/Rust error
  freshness passes; TypeScript's unversioned diagnostics remain unavailable.
- Context: [the pack replay](pack-workflow-2026-10-29.json) passes seven evidence
  checks. Three scripted retrieval operations become one, with estimated result
  tokens increasing from 343 to 426. The extra information is not free.
- Indexing: tests cover branch switch-back reuse, dirty changes, detached HEAD
  and linked worktrees. Static call relationships still miss some import aliases;
  installed LSP references remain the stronger option for those cases.
- Accounting: `python -m ctx.session_economics` imports final response usage and
  tool events. Tests reject duplicate responses, malformed counts, mismatched
  observed models and invalid savings comparisons. No provider rate is invented.
- Comparison: [pinned tool replays](tool-comparison-2026-10-29.md) exercise actual
  interfaces on common fixtures. ctx and Serena pass all three tasks. TokenSave
  passes bug fix and logs and safely refuses the ambiguous rename. Headroom
  recovers the quiet log through CCR; its editor cells are unsupported. WozCode
  is unavailable in this environment.

Live agent turns, billed savings and the WozCode host trial remain unmeasured.
These local results do not establish an end-to-end winner. No hosted model or
GPU inference was invoked for this work.
