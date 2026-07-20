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
- Bounded retrieval verbs: `ctx search`, `ctx get`, `ctx stats`, `ctx map`,
  `ctx def`/`refs`/`impact`. One tool (`ctx`), operations by parameter.
<!-- ctx-harness:end -->
