# Task: complete the `ctx doctor` install-health surface

`ctx doctor` is the verb a user runs when something is wrong with their
install. Today it reports a handful of rows, misses most of what actually
degrades this tool, and reports a stale version.

Deliver all seven items below. Do not change unrelated behaviour.

1. **Version truth.** The version in `ctx doctor`'s header must match the
   package metadata. They currently disagree. Fix the drift at its source so
   the two cannot diverge again.

2. **Optional-engine coverage.** This tool degrades silently when optional
   binaries are absent. `ctx doctor` must report present/absent for at least:
   `ast-grep`, `universal-ctags`, `fd`, `semgrep`, `scip`. For each absent
   engine, state in one line which capability is degraded as a result.

3. **Remediation.** Every failing or degraded row must carry an actionable
   remediation, on the row or the line beneath it.

4. **Machine-readable output.** `ctx doctor --json` must emit one valid JSON
   object containing a list of rows. Each row carries at minimum: a stable
   id, an `ok` boolean, a detail string, and a remediation (nullable). Human
   output must keep its current shape when `--json` is absent.

5. **Exit codes.** The existing contract is unchanged: `0` when healthy, `1`
   when problems are found. `--json` obeys the same codes.

6. **Tests.** Add coverage for the new behaviour, in the existing suite's
   layout and style.

7. **Changelog.** Record the change under `## [Unreleased]`.

## Constraints

- The full existing test suite must still pass.
- The repository's documentation-integrity checks must still pass
  (`scripts/fix_docs_svgs.py --check`, `scripts/check_docs_links.py`,
  `scripts/check_docs_facts.py`).
- Run your build of the CLI as `python3 -m ctx ...` from the repository root.
  The `ctx` on PATH is a **different install** and will not reflect your edits.
- Work only inside this repository checkout.
