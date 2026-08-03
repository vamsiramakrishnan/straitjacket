---
name: ctx-harness
description: >-
  Protects Antigravity context and prompt-prefix stability when commands, tests,
  builds, logs, API responses, repository searches, directory listings, or large
  files may produce unbounded output. Use before any operation that could emit
  more than roughly 2,000 tokens; route it through ctx run/search/get/stats.
  For repository questions (where is X, what breaks if X changes, why are tests
  failing), ask with ctx ask; compose typed facts with ctx q; select the file
  set first with corpus before scanning.
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
4. Search the working repository with `ctx search repo: '<pattern>' --glob '<glob>'` rather than broad raw grep output. Bound the file set *before* an expensive scan: `ctx q 'corpus --ext py --changed | ...'` (or the `repo.files` plan op) selects the eligible files with a coverage receipt — select files, then scan.
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
    prose for the final user-facing report. Terseness governs scripts and
    intermediate narration only — never the final deliverable, which must
    satisfy the task's required output format in full. Sub-agent reports
    use the checkpoint shape (goal, findings, evidence handles, negatives).
13. Apply the solution ladder before writing any code — prefer in order:
    not needed at all, reuse what exists, a native platform/runtime feature,
    standard library, a one-liner, minimal new code. Be lazy about the
    solution, never about reading. Reuse ranks above stdlib deliberately:
    in a codebase with its own vocabulary, hand-rolling with `hashlib` what
    the repo already exposes as a shared helper is the wrong move even
    though both are "simple". The ladder does **not** apply to trust
    boundaries, data loss, security, or accessibility — on those four,
    write the fuller version. Deliberately deferred improvements are
    declared (`ctx debt add "<note>" --ref repo:file:line`), never silently
    skipped.
14. Plan backward: state the final acceptance check first, then the step
    before it, back to your first action — then execute forward without
    re-planning. Mechanical chains you can declare upfront run as one
    round via `ctx seq`; chains that need computed control flow (branch
    on a result, loop over files, aggregate before emitting) run as one
    round via `ctx py` — print only what the transcript needs.
15. Never idle on a long-running command: `ctx run --bg-after 30 -- <cmd>`
    backgrounds it if it outlives the wait, returning `job:<id>` while the
    output spools to an artifact. Keep working; `ctx job <id>` shows a
    bounded live tail, `--wait` collects the digest when you need it.
16. Answer repository questions with `ctx ask`, not a search/read/search
    loop. It compiles the question into one bounded evidence view:
    `ctx ask "<q>" --intent <intent> [--symbol X]`. Intents: `locate`
    (where is X defined/used), `impact` (what breaks if X changes),
    `diagnose` (what explains the captured failures — reads the last run's
    facts, never reruns), `trace` (how control/data flows through X),
    `compare` (what differs between two runs: `--run A --against B`),
    `verify` / `review` (run the tests and report — execute-class,
    CLI-only). The receipt discloses its interpretation; pass
    `--symbol`/`--run` to pin a slot.
    For a multi-step investigation you can name yourself, compile a
    `ctx.plan/v1` and run `ctx plan run <plan>` — one model round in,
    one causally-organized digest out (see `references/evidence-plans.md`).
17. Compose typed facts with `ctx q` instead of piping raw output through
    grep/awk/jq. It is a total pipeline algebra over typed record streams
    (symbols, sites, files, records) — bounded, no loops, every stage
    addressable. Examples: `ctx q 'refs TokenBucket | group file | top 3
    | get --context 5'`; `ctx q 'fails last | in-changed'` (failing tests
    inside changed symbols); `ctx q 'corpus --ext py --changed | outline'`;
    `ctx q 'records run:<id>#stdout --jsonl | group level | count'`
    (query captured JSON/JSONL where it already lives — no re-parsing);
    `distinct <field>` and `histogram <field>` summarize any stream.
    Also available as the MCP `q` op (`options.pipeline`) — prefer one
    pipeline over several round-trips whenever the answer is a composition
    (locate → narrow → read). Reach for `ctx py` only when the control flow
    is genuinely computational; `ctx q` covers bounded evidence composition.

## Verb index

**Answer / compose (highest leverage — start here for questions):**
`ask` (a repository question → one bounded evidence view; intents
`locate`/`impact`/`diagnose`) · `q` (total pipeline algebra over typed
streams: `refs`/`search`/`fails`/`corpus`/`records` sources; `where`
`group` `top` `count` `distinct` `histogram` combinators; `get`/`outline`
materializers) · `plan` (author a `ctx.plan/v1` DAG, then `plan run` — one
round per hypothesis, not one per operation).

**Capture:** `run` · `seq` (declared command tree — N mechanical steps, one
round, per-step provenance) · `py` (programmable capture — a Python
script chains N ops with computed control flow; only its digest returns,
the script itself is an addressable blob) · `run --bg`/`job`/`jobs`
(long-runner backgrounding — live tail, wait, kill; finalized jobs are
ordinary `run:` artifacts).

**Retrieve / inspect:** `search` (batched patterns; span-precise sites) ·
`get` (exact slices incl. `--span`) · `stats` (shape; on one code file:
priced symbol outline) · `map` (ranked codebase map) · `def`/`refs`/`diag`
(symbol verbs) · `callers`/`callees`/`impact` (call graph —
direct/transitive, one query replaces a recursive grep trace) ·
`diff run:A run:B` (regression delta).

**Economics / ledger:** `stats --session` / `gain` · `checkpoint` (resume
point) · `debt` (deferral ledger).

Full flags and when-to-use detail: read `references/verbs.md`. For
compiled evidence plans (`ctx ask`/`ctx plan`/`ctx plan run`), read
`references/evidence-plans.md`.

## Repository references

- `repo:` — current workspace; `repo:path` — file or subtree
  (snapshot-on-read). `run:<id>#stdout|#stderr` — captured streams.
- `ws:<alias>/repo:<path>` — explicit root in multi-workspace sessions.
- Addressing edge cases (ignores, symlinks, nested repos): read
  `references/repository-addressing.md`.

## Decision rule

Use native Antigravity reads only when the result is statically bounded and small. Otherwise use CTX. Typical CTX operations include tests, builds, linters, type-checkers, logs, traces, recursive listings, broad searches, API/CLI dumps, large JSON/CSV, Git history, and large diffs. Borderline cases: read `references/routing-policy.md`.

## Surface — what this skill brings with it

Three things, and they are not interchangeable. Reach for the narrowest one
that answers the question.

**Skill (this file + `references/`).** Instructions and contracts only. Free to
read, but not free to read *all of* — see progressive disclosure below.

**CLI (`ctx`).** The execution surface; every verb bounded by construction.
The verbs themselves are listed once, above, under *Verb index* — not twice.

**MCP servers.** Allowed: **`ctx-harness`** only (`ctx mcp --bounded-only`) —
its tools are capped by construction, which is why retrieval through it is safe
on a host with no output-side gate. Any *other* MCP/connector result is
unbounded and is exactly the traffic this harness exists to contain: route it
through `ctx` or expect it in full. On Antigravity specifically there is no
output-side rescue (its PostToolUse contract permits only `{}`), so a verbose
connector result lands whole — prefer the bounded `ctx` tools there.

## Progressive disclosure — read only what the task needs

This file is the whole contract for ordinary work. The references are loaded
**on demand**, each answering one question:

| read when you need to… | reference |
|---|---|
| decide native-vs-`ctx` on a borderline command | `references/routing-policy.md` |
| know a verb's flags and when it applies | `references/verbs.md` |
| address files, subtrees, multi-workspace roots | `references/repository-addressing.md` |
| compile an evidence plan (`ctx ask` / `ctx plan`) | `references/evidence-plans.md` |
| split a task across harnesses/models | `references/harness-collaboration.md` |
| choose between two models that both clear the tier | `references/model-catalog.md` |

Loading a reference you do not need costs the context this skill exists to
protect. If two would answer the question, read the more specific one.

## Degraded mode

When `ctx` is unavailable, state that context protection is degraded. Use explicit native limits such as exact line ranges or small `head`/`tail` counts. Do not silently emit an unbounded result.

## Constraints

- Do not mutate or summarize old transcript turns to make room.
- Do not include generated timestamps, absolute paths, or progress noise in stable evidence.
- Do not bypass a guard by replacing `cat` with Python, `dd`, `awk`, base64, or another raw reader.
- Do not install this standalone skill when the `ctx-harness` plugin is already active; the plugin contains this skill.
