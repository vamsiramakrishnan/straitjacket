<sub><a href="README.md">« straitjacket / docs</a></sub>

# How straitjacket works

A plain-language walkthrough. No prior vocabulary assumed; every term is
introduced when it appears. Ten minutes to read; the deep-dive docs are
linked at each step for when you want more.

## The problem, in one paragraph

Coding agents pay for their context window **twice**: once when tool output
enters it, and again *every subsequent turn*, because the whole transcript
is re-sent to the model each round. One noisy `pytest` run can dump 300k
tokens into the transcript; a routine `kubectl get pods` is thousands more.
You pay for those tokens on every turn that follows, your session slows
down, and when the window fills, compaction summarizes old content away —
sometimes deleting the one line that mattered, with no trace it existed.

straitjacket's answer: **never let the flood into the transcript in the
first place, and never destroy a byte.** Raw output goes into a local
content-addressed store; the agent sees a small, deterministic summary
(a *digest*) in which every omitted byte keeps an exact address it can be
retrieved from later.

## Walking one command through the system

Say your agent (or you) runs a failing test suite:

```bash
ctx run -- pytest -q
```

**Step 1 — capture.** The command executes normally, but its stdout/stderr
stream into the artifact store (a local SQLite + blob store in your user
state, never committed). The raw bytes are now permanent and
content-addressed: nothing downstream can lose them.

**Step 2 — digest.** A *profile* — a parser matched to this kind of output
(pytest, cargo, docker tables, JSON, generic logs…) — extracts what
matters and renders a bounded digest:

```
[ctx run:8d8335db6848 profile=pytest/v2]
command: pytest -q
exit: 1
stdout: 4,102 lines · 402.1 KiB · est 98,000 tokens
failing tests (census):
  1. tests/test_auth.py::test_token_expiry   tests/test_auth.py:42
coverage:
  census: 1/1 identities inline · attested complete
  shown: 1 spans · omitted: 4,098 lines
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```

Read it top to bottom: what ran, what it cost in raw form (the ~98k tokens
your agent **didn't** pay), the complete list of failures with file:line
coordinates, an honest account of what was omitted, and a ready-made
command to pull any omitted region. The digest is a fixed size no matter
how large the output was.

**Step 3 — retrieval, only if needed.** If the agent wants the traceback
detail, it pays a few hundred tokens for exactly the slice it needs:

```bash
ctx get run:8d8335db6848#stdout --lines 1280:1300
```

That's the whole core loop: **capture everything, show a summary, keep an
address for the rest.** Formally it's a two-layer code — a cheap always-on
layer plus an exact on-demand layer — and [THEORY.md](THEORY.md) states the
objective it optimizes, but you don't need the theory to use it.

## Why the digest is deterministic (and why you should care)

Identical bytes always produce a byte-identical digest: timings, temp
paths, ANSI colors, and locale noise are stripped. This matters for money —
model providers cache your prompt prefix, and cached tokens cost ~10× less
than fresh ones. A transcript full of stable digests keeps the cache warm
(measured 96–98% hit rates vs 80–84% for tools that rewrite history).
It also means two runs of the same failing test look the same, so the
*differences* that appear are real signal (`ctx diff run:A run:B`).

## How it hooks into your agent

You never call `ctx run` yourself in normal use. `ctx wrap setup` registers
the harness with your agent host:

- **Before a tool call runs** (PreToolUse): a fast classifier looks at the
  command. Known-flooding commands (`pytest`, `cat bigfile`, package and
  cloud CLIs…) are transparently rewritten to run through `ctx run` — the
  agent gets the digest instead of the flood. Known-safe commands pass
  through untouched. Unknown commands follow a conservative policy you can
  tune in `ctx.toml`.
- **After a tool call returns** (PostToolUse): a safety net. If something
  oversized got through anyway, the result is captured and replaced by a
  digest before it reaches the model.
- **A retrieval tool** (via MCP): the agent gets one bounded `ctx` tool for
  search/get/stats over the store and the repo — retrieval that *cannot*
  flood, because every operation is capped.

All three hosts (Antigravity, Claude Code, Codex) get the same three
pieces, translated to each host's native config format. Nothing here
requires trusting the model to follow instructions — the hooks are
mechanical.

## What else is in the box (each optional, each measured)

- **`ctx search / get / stats / map`** — bounded retrieval over captured
  artifacts *and* your repository (exact line/byte/symbol slices, ranked
  repo maps). The agent explores without `cat`-ing files into context.
- **`ctx q`** — small pipelines over typed evidence
  (`refs Foo | group file | top 3 | get`), executed locally in one step.
- **`ctx plan` / `ctx investigate`** — the agent writes a short JSON plan
  (a bounded DAG of evidence operations); the harness validates, prices,
  executes it locally, and returns **one** digest instead of N rounds of
  tool calls. Measured: a 6-round investigation collapsed to 1.
- **`ctx replay`** — learn from recorded sessions, offline: which digests
  kept the evidence agents actually used (`--regret`), and which evidence
  agents observably followed up on (`--outcomes`). Both feed reviewable,
  committed policy files — the harness never adapts silently at runtime.

## What it does NOT do

- **It doesn't make the model smarter.** In A/Bs, task success is at
  parity; the wins are cost, latency, turns, and evidence preservation.
- **It doesn't always win.** When output is small, digests pass it through
  ~1:1; when a flood is trivially greppable, a shell-savvy agent can beat
  the harness's overhead — that regime is published in our own evals, not
  hidden. Graduated engagement keeps the harness out of your way on small
  sessions.
- **It doesn't phone home, learn online, or rewrite your transcript.**
  Capture is local, policy changes are compiled offline into a diffable
  TOML you review and commit, and history is never edited — old content is
  *elided behind addresses*, never deleted.

## Where to go next

| You want | Read |
|---|---|
| to install and try it | [GETTING-STARTED.md](GETTING-STARTED.md) |
| the vocabulary (artifact, span, digest, contract) | [CONCEPTS.md](CONCEPTS.md) |
| the numbers behind every claim | [`evals/`](../evals/) — one receipt per claim |
| why retrieval choices carry price tags | [PRICED-CONTEXT.md](PRICED-CONTEXT.md) |
| the formal objective and its theorems | [THEORY.md](THEORY.md) |
| to write a digest profile for your own tool | [WRITING-A-PROFILE.md](WRITING-A-PROFILE.md) |
