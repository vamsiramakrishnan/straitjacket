---
name: ctx-harness
description: >-
  Protects Antigravity context and prompt-prefix stability when commands, tests,
  builds, logs, API responses, repository searches, directory listings, or large
  files may produce unbounded output. Use before any operation that could emit
  more than roughly 2,000 tokens; route it through ctx run/search/get/stats.
metadata:
  category: efficiency
---

# CTX Context Artifact Harness

## Goal

Keep the transcript bounded and append-only. Full payloads live in CTX artifacts; the conversation receives only deterministic digests and exact evidence slices.

## Required behavior

1. Resolve the target workspace before using CTX. Prefer the current repo or the workspace containing the requested path.
2. Run potentially noisy commands as `ctx run -- <command> <args...>`. Add `--focus '<question>'` when the user's intent is specific.
3. Search command output with one batched call: `ctx search <ref> '<p1>' '<p2>' ...`.
4. Search the working repository with `ctx search repo: '<pattern>' --glob '<glob>'` rather than broad raw grep output.
5. Read only exact evidence using `ctx get <ref> --lines A:B`, `--bytes`, `--records`, `--json-pointer`, `--symbol`, or `--span <token>` (span tokens appear in digests at omission points; resolving one is always bounded).
6. Use `ctx stats <ref>` for schema, counts, heavy hitters, and structured summaries.
7. Treat artifact handles and coordinates as evidence citations. Preserve them in plans, findings, and checkpoints.
8. Never re-run a command merely to recover output already stored under a handle.
9. Never inspect the CTX backing-store path directly and never paste a complete blob into the conversation.
10. Prefer one multi-pattern search over serial single-pattern searches.
11. Cite evidence by handle and coordinate (`run:<id>#stdout L14238`), never
    by re-quoting it: quoted evidence duplicates context and burns output
    tokens, while the citation resolves exactly for any reader.
12. Keep intermediate narration to one terse line per step; reserve full
    prose for the final user-facing report. Sub-agent reports use the
    checkpoint shape (goal, findings, evidence handles, negatives).
13. Apply the solution ladder before writing any code — prefer in order:
    not needed at all, reuse what exists, standard library, a one-liner,
    minimal new code. Be lazy about the solution, never about reading.
    Deliberately deferred improvements are declared
    (`ctx debt add "<note>" --ref repo:file:line`), never silently skipped.

## Verb index

`run` (capture) · `search` (batched patterns) · `get` (exact slices incl.
`--span`) · `stats` (shape; on one code file: priced symbol outline) ·
`map` (ranked codebase map) · `def`/`refs`/`diag` (symbol verbs) ·
`diff run:A run:B` (regression delta) · `stats --session` / `gain`
(economics) · `checkpoint` (cache epoch) · `debt` (deferral ledger).
Full flags and when-to-use detail: read `references/verbs.md`.

## Repository references

- `repo:` — current workspace; `repo:path` — file or subtree
  (snapshot-on-read). `run:<id>#stdout|#stderr` — captured streams.
- `ws:<alias>/repo:<path>` — explicit root in multi-workspace sessions.
- Addressing edge cases (ignores, symlinks, nested repos): read
  `references/repository-addressing.md`.

## Decision rule

Use native Antigravity reads only when the result is statically bounded and small. Otherwise use CTX. Typical CTX operations include tests, builds, linters, type-checkers, logs, traces, recursive listings, broad searches, API/CLI dumps, large JSON/CSV, Git history, and large diffs. Borderline cases: read `references/routing-policy.md`.

## Degraded mode

When `ctx` is unavailable, state that context protection is degraded. Use explicit native limits such as exact line ranges or small `head`/`tail` counts. Do not silently emit an unbounded result.

## Constraints

- Do not mutate or summarize old transcript turns to make room.
- Do not include generated timestamps, absolute paths, or progress noise in stable evidence.
- Do not bypass a guard by replacing `cat` with Python, `dd`, `awk`, base64, or another raw reader.
- Do not install this standalone skill when the `ctx-harness` plugin is already active; the plugin contains this skill.
