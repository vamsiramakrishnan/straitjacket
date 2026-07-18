# CTX verb reference

Full flag detail for verbs the skill body indexes. Read this when a one-line
index entry is not enough; each verb's output is bounded and deterministic.

## Capture and retrieval

- `ctx run -- <cmd> [args...]` — execute with birth-time capture; transcript
  receives a digest with span tokens at omission points. `--focus '<q>'`
  biases evidence selection. Failing runs get a larger digest budget than
  successes by policy (failure is evidence; success is boilerplate).
- `ctx search <ref> '<p1>' ['<p2>' ...] [--context N] [--glob G] [--fixed]`
  — one batched multi-pattern search over run:/blob:/repo: refs.
- `ctx get <ref> --lines A:B | --bytes A:B | --records A:B |
  --json-pointer /p | --symbol Name.dotted | --span <token>` — exact
  bounded slices with provenance. Span tokens come from digests and always
  resolve bounded.
- `ctx stats <ref>` — schema/shape statistics. On a single code file this
  returns the priced symbol outline: every symbol with line range, ~token
  price, and a span handle — prefer it over reading a large file.

## Round economy

- `ctx seq '<cmd1>' '<cmd2>' ... [--keep-going] [--focus q]` — declared
  command tree: N steps execute in one round with `&&` semantics. Every
  step is a full birth-gate capture addressable as `run:<id>`; a failing
  step's digest rides in full, a green tree stays terse. Use for
  mechanical chains you can declare upfront (test → build → lint):
  measured, 65–70% of repair/creation rounds were such chains.

## Repository comprehension

- `ctx map [--budget N] [--focus term]` — ranked, budget-fitted codebase
  map; entries carry token price and definition count.
- `ctx def repo:<path>:<Symbol>` / `ctx refs <Symbol>` — definition site /
  reference sites, snapshot-backed with spans.
- `ctx diag [path]` — deterministic lint/syntax digest.
- `ctx diff run:A run:B` — regression delta between two captured runs
  (failure-set and template changes, with spans).

## Session economics

- `ctx stats --session` — wire scorecard: token classes, cache hit,
  cold-prefix vs true invalidations, timing split, effort mix.
- `ctx gain` — cumulative containment savings by verb.
- `ctx checkpoint` — freeze task state into a new cache epoch.

## Judgment ledger

- `ctx debt add "<note>" [--ref repo:file:line]` — declare a deliberately
  deferred improvement instead of silently skipping it.
- `ctx debt list` / `ctx debt resolve <id>` — review and close.

## Call graph (Python, ast — zero-dep, always-current)

- `ctx callers <Symbol>` — direct callers of a function/method, each with `file:line`.
- `ctx callees <Symbol>` — the in-repo functions it calls.
- `ctx impact <Symbol> [--depth N]` — transitive callers (blast radius): everything
  that reaches the symbol, grouped by hop distance. Use before changing a shared
  function — one query replaces recursive grep. Name-resolved (ambiguous names
  report every candidate); deterministic; worktree-hash cached, no daemon.
