<!-- ctx-harness:start -->
## Context containment (straitjacket / ctx)

This workspace is harnessed by **ctx** (straitjacket). Potentially unbounded
command and file output is captured at the source and returned as a small,
deterministic digest that keeps an exact retrieval address for every omitted
byte. Work *with* it — don't try to defeat the containment by paging or
pre-filtering; the digest already selects the load-bearing evidence.

- Run noisy commands normally. The harness routes floods (test suites, builds,
  `grep`/`cat`/`git diff`, package and cloud commands) through `ctx run` and
  returns a bounded digest. Small outputs pass through untouched.
- To pull exact omitted bytes, use the `ctx` MCP tool — `op:"get"`,
  `ref:"run:<id>#stdout"`, `selector:{"lines":"A:B"}` — or a `span:`/`--symbol`
  token printed in the digest. Retrieval is bounded; it cannot re-flood context.
- Cite coordinates (`file:line`, `run:`/`span:` handles) instead of restating
  file or tool output back into the conversation.
- Answer repository questions with `ctx ask "<q>" --intent <intent>` — one
  bounded evidence view instead of a search/read/search loop. Intents:
  `locate`/`impact`/`trace` use `--symbol X` unless the question contains one
  unambiguous identifier; `diagnose` reads the last run's failure facts and
  never reruns; `compare` compares execution receipts (`--run A --against B`),
  not arbitrary concepts; `verify`/`review` run tests (execute-class, CLI-only).
  For a multi-step investigation you can name, run a compiled `ctx.plan/v1`
  via the `investigate` op.
- Compose typed facts with `ctx q '<stage> | <stage>'` (a bounded, total
  algebra) instead of piping raw output through grep/awk/jq: e.g.
  `refs X | group file | top 3 | get`, `fails last | in-changed` (failing
  tests in changed symbols), `corpus --ext py --changed | outline` (bound
  the file set before scanning), `records run:<id>#stdout --jsonl | group
  level | count` (query captured JSON where it lives).
- Bounded retrieval verbs: `ctx search`, `ctx get`, `ctx stats`, `ctx map`,
  `ctx def`/`refs`/`impact`. One tool (`ctx`), operations by parameter.
<!-- ctx-harness:end -->
