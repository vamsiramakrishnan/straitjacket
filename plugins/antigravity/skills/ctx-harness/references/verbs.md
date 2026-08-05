# CTX verb reference

Full flag detail for verbs the skill body indexes. Read this when a one-line
index entry is not enough; each verb's output is bounded and deterministic.

## Answer a question / compose facts (start here)

- `ctx ask "<question>" --intent <intent> [--symbol X] [--run r]
  [--against B] [--command C] [--depth N] [--plan]` — compile a repository
  question into one bounded evidence view (a typed `ctx.plan/v1` preset run
  through the plan tier). Intents: `locate` (where is X defined/used),
  `impact` (what breaks if X changes — callers + blast radius + related
  tests), `diagnose` (what explains the captured failures — reads the last
  run's facts and joins against the change set; **never reruns tests**),
  `trace` (structural call path through X — callers/callees/reach),
  `compare` (behavioral delta between two runs: `--run A --against B`),
  `verify` (run the tests covering the change and report), `review`
  (changed symbols + tests + a fresh run + root-cause join). `verify` and
  `review` are **execute-class** (they run tests) — CLI-only; the bounded
  MCP tier rejects them. No natural-language guessing: `--intent` is
  required (a missing one teaches and suggests), and the subject is
  `--symbol` or the question's single identifier-shaped token (disclosed).
  `--plan` prints the compiled plan without executing.
- `ctx q '<stage> | <stage> | …'` — total pipeline algebra over typed
  record streams; bounded, no loops, every stage's result minted as an
  addressable `blob:`. Sources open a stream: `refs <Sym>`, `search <pat>
  [--glob G]`, `callers/callees/impact <Sym>`, `fails [run:|last]`,
  `corpus [--ext E]… [--glob G]… [--exclude G]… [--changed] [--max N]`
  (the eligible file set with a coverage receipt — `--changed` binds to
  worktree generations, never mtime), `records <run:|blob:> [--jsonl]
  [--pointer /p]` (a stored JSON/JSONL artifact as records — query
  compiler/test/SARIF/lockfile output where it already lives, no
  re-parsing). Combinators: `where <field><op><val>` (= != ~), `group
  <field>`, `top <N>`, `count`, `distinct <field>`, `histogram <field>
  [--buckets N]`. Materializers (terminal): `get [--context N]`, `outline`.
  The root-cause one-liner: `ctx q 'fails last | in-changed'` — failing
  tests inside symbols changed this generation. Prefer `ctx q` over piping
  raw output through grep/awk/jq/sort/uniq; reach for `ctx py` only when
  the control flow is genuinely computational.

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
- `ctx py '<python script>' | --file <path> | -` (stdin/heredoc) —
  programmable capture: when the chain needs computed control flow
  (branch on a result, loop over files, aggregate before emitting), write
  a short Python script instead of N rounds of tool calls. It runs under
  the birth gate (`python -I`, script fed on stdin) and only its bounded
  digest returns — print exactly what the transcript needs; intermediates
  stay local. That terseness scopes to the script's output only: the final
  user-facing answer must still satisfy the task's required output format
  in full. The script itself is stored and cited as `blob:<id>` in the
  digest header (reproduce: `ctx get blob:<id> | python3 -I -`); both
  streams stay span-addressable. Sub-steps that deserve their own handles
  call `ctx run` from inside the script. Isolated mode means repo imports
  need an explicit `sys.path.insert(0, ".")`. Same trust envelope as
  `ctx run`; failing scripts get the failure digest budget (traceback is
  evidence, and frames say `File "<stdin>"` — never a host path).

## Long runners

- `ctx run --bg | --bg-after T -- <cmd>` — supervised backgrounding: the
  run starts under a detached supervisor either way. Finishes within `T`
  → the normal digest returns as if foreground (byte-identical, same
  `run:` id). Still running at `T` → the transcript gets `job:<id>`
  immediately and the output exists only as a spooled artifact. Inspect
  with `ctx job <id>` (bounded live tail, never a flood), `--wait`
  (block, then digest), `--kill` (SIGKILL the group; what spooled is
  finalized and addressable); `ctx jobs` lists. Finalized jobs are
  ordinary `run:` artifacts — `search`/`get` address them identically.
  Never idle a session on a long process: background it, keep working,
  collect the digest when you need it.

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

## Compiled evidence plans (one model round per hypothesis epoch)

- `ctx plan ops` — the registered logical operators (kinds, capability
  class, cost class, engine availability). Read this before authoring.
- `ctx plan validate <plan.json|->` / `ctx plan price …` — static totality
  check with typed rejections · the pre-execution cost card. Nothing runs.
- `ctx plan run <plan.json|->` — execute a `ctx.plan/v1` DAG (repo.changed,
  test.run, evidence.join, ast.search, semantic.taint, …) locally; ONE
  ranked investigation digest returns: conclusion candidates with plane
  attribution, counterevidence, coverage, per-node `blob:` addresses.
- `ctx plan run <plan.json|->` — the epochal loop: same execution plus
  the replan allowance (default 1; exceeding it is declared, recorded, and
  argues for patch/verify instead of another sweep).
- Use a plan when you would otherwise run 3+ exploration commands whose
  sequence you can already name; use interactive verbs when each result
  changes what you would do next.

## Call graph (Python, ast — zero-dep, always-current)

- `ctx callers <Symbol>` — direct callers of a function/method, each with `file:line`.
- `ctx callees <Symbol>` — the in-repo functions it calls.
- `ctx impact <Symbol> [--depth N]` — transitive callers (blast radius): everything
  that reaches the symbol, grouped by hop distance. Use before changing a shared
  function — one query replaces recursive grep. Name-resolved (ambiguous names
  report every candidate); deterministic; worktree-hash cached, no daemon.
