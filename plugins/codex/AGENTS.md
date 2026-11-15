<!-- ctx-harness:start -->
## Context containment (straitjacket / ctx)

This workspace is harnessed by **ctx** (straitjacket). Unbounded command and
file output comes back as a small deterministic digest with an exact
retrieval address for every omitted byte. Work *with* it — don't page or
pre-filter around it.

- Run noisy commands normally: floods (tests, builds, `grep`/`cat`/`git
  diff`, package and cloud commands) come back as a bounded digest.
- To pull exact omitted bytes, use the `ctx` MCP tool — `op:"get"`,
  `ref:"run:<id>#stdout"`, `selector:{"lines":"A:B"}` — or a `span:`/`--symbol`
  token printed in the digest. Retrieval is bounded; it cannot re-flood context.
- Cite coordinates (`file:line`, `run:`/`span:` handles), not restated output.
- Digest excerpts are verbatim with real line numbers; "(exact)" counts are
  exact; "every error-like line is one of these N messages" is true; a
  "varies" message stands for several values, listed on its row or by its
  command. Fetch only what is not shown.
- Answer repository questions with `ctx ask "<q>" --intent <intent>` — one
  bounded evidence view instead of a search/read/search loop. Intents:
  `locate`/`impact`/`trace` use `--symbol X` unless the question contains one
  unambiguous identifier; `diagnose` reads failures of a test run
  captured by `ctx run`, never reruns; `compare` compares execution receipts
  (`--run A --against B`); `verify`/`review` run tests (execute-class,
  CLI-only). For a multi-step investigation you can name, run a compiled
  `ctx.plan/v1` via the `investigate` op.
- Compose typed facts with `ctx q '<stage> | <stage>'` (a bounded, total
  algebra) instead of piping raw output through grep/awk/jq: e.g.
  `refs X | group file | top 3 | get`, `corpus --ext py --changed | outline`.
  Only for output captured by `ctx run`: `fails last | in-changed`,
  `records run:<id>#stdout --jsonl | group level | count`.
- For an existing file: grep, read windows, or
  `ctx search repo:<file> PATTERN`.
- Bounded retrieval verbs: `ctx search`, `ctx get`, `ctx stats`, `ctx map`,
  `ctx def`/`refs`/`impact`.
<!-- ctx-harness:end -->
