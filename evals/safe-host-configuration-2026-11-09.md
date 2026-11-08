# Safe host configuration: implementation and regression evidence

Date: 2026-11-09. Base: `e23ce52fb65a45d345bf1a9fa4d17344d6d8bbe6`
(PR #50, v0.43.0). Implementation branch: `feat/safe-host-config`.
The initial validation below describes commit `8b08e9267d52f54686a0346da676c64283ca9295`.
The PR review follow-up is recorded separately at the end.

## Result

Codex and OpenCode use one source-aware configuration plan for preview,
installation, repair and removal. Existing host detection, workspace policy,
readiness receipts, setup narration and capability declarations remain in place.
No runtime dependency was added; the hook hot path is unchanged.

| Surface | Result |
|---|---|
| Codex TOML | Merge missing keys into regular/dotted/quoted tables and inline tables; retain comments, other MCP servers, model settings and existing TUI preferences. Add `features.hooks` only when absent; an explicit false value conflicts. |
| Codex hooks | Register and verify PreToolUse, PostToolUse and SessionStart independently. Repair a missing stage without duplicating others. Preserve third-party hook groups, including hooks sharing an existing group. |
| OpenCode JSON/JSONC | Update the selected project config in place, preserving comments, trailing commas, other servers and plugins. Never create a shadow JSON file beside JSONC. Two project configs refuse as ambiguous. |
| Conflicts | Produce exact unified diffs against the original bytes. `--apply` cannot override conflicting user values, explicit disables, remote Codex transport, modified owned source or an unmanaged plugin. |
| Ownership | `.ctx/host-config.json` tracks individual contributions, value-source hashes and inverse edit fragments. A file banner never authorizes a whole-file replacement. Pre-existing matching entries are not claimed. |
| Writes | Validate the full parsed result after every source edit. Stage all files, preserve modes, check all snapshots before replacement, replace each file atomically and roll back on failure. A concurrent user edit is preserved during rollback. |
| Removal | Restore untouched files exactly or remove individual owned entries after unrelated edits. Preserve settings and integrations added later. Modified owned values or comments inside those values require reconciliation. |
| Readiness | Fingerprint ownership state along with existing managed surfaces. Doctor checks every Codex component and OpenCode JSONC plus native hooks, including files removed from a previously tracked installation. |

## Commands

```bash
ctx setup --host codex --host opencode --preview
ctx setup --host codex --host opencode --apply
ctx doctor
ctx setup --host codex --repair
ctx setup --host codex --host opencode --remove
ctx setup --host codex --host opencode --remove --apply
```

Preview requires explicit Codex/OpenCode hosts and writes neither configuration
nor readiness receipts. Default setup stops before changing a pre-existing,
unowned configuration. Fresh installs retain the existing setup flow. The plain
setup output and `ctx wrap` entrypoints cannot bypass the review gate.

The CLI rebuilds a plan from current files on each invocation, displays that
plan, and checks its snapshots again before writing. It does not persist a plan
token between separate preview and apply commands.

Invalid syntax and duplicate keys refuse with a file/position diagnostic; a
trustworthy structural diff requires a parseable source document. Valid but
conflicting configuration receives an explicit replacement proposal without
being written. Doctor checks configuration, not whether an already-running
agent has reloaded it.

## Implementation

| File | Responsibility |
|---|---|
| `src/ctx/config_edit.py` | JSONC token spans, TOML source indexing backed by `tomllib`, lossless insertion/deletion, complete semantic validation and exact value-source fragments. |
| `src/ctx/host_config.py` | Host requirements, shared plans/diffs, conflict handling, ownership, guarded writes/rollback and removal. |
| `src/ctx/installer.py` | Codex installer and complete MCP/hooks/instruction doctor contracts. Shell-safe executable paths and JSON-safe hook rendering. |
| `src/ctx/mcp_hosts.py`, `src/ctx/native_hooks.py` | OpenCode integration, JSONC checks and native-plugin validation. Other host adapters retain their existing schemas and capabilities. |
| `src/ctx/wrap.py`, `src/ctx/commands/hosts.py`, `src/ctx/cli.py` | Review gate, preview/apply/remove flags, existing guided setup and verification. |
| `src/ctx/setup_telemetry.py` | Include the ownership ledger in the content-free readiness fingerprint. |
| `tests/test_host_config_flow.py` | 73 isolated acceptance cases for the configuration flow. |
| `scripts/check_distribution.py` | Exercise preview/apply/repeat/remove from an installed wheel outside the checkout. |

## Regression results

Runtime: CPython 3.12.14, Linux, base dev dependencies. Tests isolate host HOME
and configuration files; no host CLI/model/provider session is required.

| Check | Result |
|---|---|
| New configuration acceptance cases | 73 passed |
| Setup/integration regression selection, builtin engines | 209 passed |
| Complete acceptance suite | 3,051 passed; 106 skipped; 16 failed |
| Complete suite, builtin engine configuration | 3,051 passed; 106 skipped; 16 failed |
| Wheel and sdist build; metadata validation | Passed |
| Clean installed-wheel smoke, including host-config lifecycle | Passed |
| Wheel implementation bytes compared with checkout | Matched |
| Documentation facts with token receipt replay | Passed |
| Documentation links | 423 references checked, passed |
| SVG normalization and generated evidence visuals | Passed |
| Documentation site build | 36 pages, passed |
| `git diff --check` | Passed |

The complete-suite failures are the same 16 failures reproduced on untouched
`main`: 15 language-server tests cannot create AF_UNIX sockets in this execution
environment (`PermissionError: [Errno 1] Operation not permitted`); the existing
orphan-process test fails its initial grandchild-liveness assertion when its
full test file runs. The process test passes in isolation. Neither subsystem
was modified. Optional engine/native/provider checks account for the skips.
The complete suite is therefore **not green in this environment**.

Baseline evidence:

- Untouched-main LSP selection: 15 failed, 17 passed, 1 skipped.
- Untouched-main `tests/test_improve_round.py`: 1 failed, 62 passed, 2 skipped.
- The union of those failing test identifiers exactly matches the final full
  suite's failure set; see `safe-host-configuration-2026-11-09.txt` for the comparison.

The focused suite covers repeated install, existing settings and integrations,
TOML quoted/dotted/inline tables and multiline strings, CRLF, JSONC comments and
trailing commas, source annotations inside owned values, explicit conflicts,
partial setup, repair, exact applicable diffs, clean removal, settings added
later, symlinks, stale snapshots, file modes, executable paths with spaces and
quotes, failure rollback and concurrent edits. It also checks that config
secrets are not copied into the ownership ledger and readiness receipts remain
content-free.

An existing legacy test initially created a generated Antigravity status-line
setting in this disposable environment's home. That test-created file was
verified and removed. The suite now uses a separate host HOME for every test;
package smoke checks also use an isolated HOME. Live Codex/OpenCode settings
were never modified, and no external host session was used for validation.

## Reproduce

```bash
pip install -e '.[dev]'
python -m pytest tests/test_host_config_flow.py -ra
python -m pytest tests/ -ra
CTX_SEARCH_ENGINE=python CTX_MAP_ENGINE=builtin CTX_CODE_ENGINE=ast \
  python -m pytest tests/ -ra
python -m build
python scripts/check_distribution.py dist/*.whl dist/*.tar.gz
```

The local build retains the base release version and is a review build,
not a new published release. No live-host compatibility claim is added:
Codex and OpenCode retain their documented contract-test/live-receipt status.


## PR #51 review follow-up: owned instruction blocks

Review `5819959144` identified that removal trusted AGENTS.md markers without
verifying the installed block source. An instruction added inside those markers
could therefore be deleted. The same missing boundary also affected repair.

The ledger now records a separate `block_hash` for the marker-inclusive source.
Repair and removal verify it before changing that block. User edits inside the
block produce a blocked plan and an exact diff; surrounding edits remain
removable. Legitimate ctx-owned block upgrades refresh the scoped hash. Older
whole-file ownership records acquire a block hash only while their recorded file
is byte-identical; edited legacy files cannot silently acquire new provenance.

Validation used isolated configuration fixtures and host HOME:

- The review reproducer selection failed 13 cases on the reviewed commit.
- All 19 new review cases pass, including actual CLI removal, new/existing
  AGENTS.md files, edits/additions/comments inside the block, outside edits,
  generated guidance upgrades, safe legacy migration and repeated removal.
- The complete setup/integration selection passes: 228 normal and 228 builtin
  engine checks. The configuration-flow file now contains 92 acceptance cases.
- Wheel/sdist build and clean installed-wheel lifecycle smoke passed; the wheel
  contains the updated host-config source. `git diff --check` passed.

The full-suite results above remain the initial implementation's recorded runs;
this follow-up ran the relevant regression selection and package check.
