<sub><a href="README.md">« straitjacket / docs</a></sub>

# ctx ask: intents as typed plan presets

> **Design & internals — not product documentation.** This doc explains *why* a
> mechanism exists and how it was reasoned out; it may describe an idea before it
> ships or record one that was rejected. For what the product does **today**,
> prefer [`spec/`](../spec/) and the [changelog](../CHANGELOG.md), and read any
> status label literally. Want to *use* `ctx ask`? The [CLI guide](CLI.md) has
> the seven intents and examples; this doc is the design behind them.

**Status:** M-L core intents shipped in v0.27.0. This page records the design
and implementation choices; use [the CLI guide](CLI.md) for operating syntax.

`ctx ask` fills a typed plan template for a selected intent and executes it
through the existing plan engine. It returns candidate conclusions,
counterevidence, coverage, and retrieval addresses. It does not use a model to
classify arbitrary natural-language requests.

<a id="the-one-line-thesis"></a>
## Plan selection

The caller selects the intent. The compiler fills its typed slots and the
existing executor performs the plan. This reduces the number of command
choices exposed to the caller without adding another executor or changing
the evidence contract.

## What an intent actually is

An intent is a frozen `ctx.plan/v1` template with typed slots, implemented
in `src/ctx/ask.py`:

```
ctx ask "Why is test_build failing" --intent diagnose
        │
        ▼ compile_ask (pure, total, deterministic)
   ctx.plan/v1  {changes · evidence.failures · join · counter-join · context}
        │
        ▼ execute_plan (SHIPPED, tier=cli)
   investigate/v1 digest  {conclusion candidates · counterevidence · coverage · next}
```

Because compilation is `json.dumps(sort_keys=True)` over the slots, the
same slots always produce the same plan id — so **every node-cache key is
stable across phrasings** that resolve to the same slots. Determinism is
inherited from the plan tier, not re-implemented.

Seven intents ship (the observe five plus two execute-class):

| intent | question | plan shape | class |
|---|---|---|---|
| `locate` | where is X defined and used? | refs → warmed symbol rows → per-file census → definition bodies | observe |
| `impact` | what could break if X changes? | callers → bounded blast radius → related tests → changes | observe |
| `diagnose` | what explains the captured failures? | changes × **captured** failures → root-cause join → counter-join → failing-frame context | observe |
| `trace` | how does control/data flow through X? | X's sites → callers (in) → callees (out) → transitive reach (hop-grouped) | observe |
| `compare` | what differs between two runs? | `evidence.diff` (the shipped run-diff as a node) | observe |
| `verify` | what proves this change is correct? | changes → related tests → **run the suite** (birth gate) | execute |
| `review` | what changed, what is risky, what is under-verified? | changes → symbols → tests → **run** → root-cause join + counterevidence | execute |

The observe five are **observe-class end to end** — `diagnose` reads
`evidence.failures` (captured facts), it never reruns tests. `verify` and
`review` are **execute-class**: they run tests under the birth gate, so
they are CLI-only — the plan validator rejects `test.run` on the bounded
MCP tier (`execute_on_observe_tier`), and the intent discloses its class
up front. `compare` needs two run refs (`--run A --against B`); its slots
teach when missing, like every other intent's subject.

Two invariants are structural, not optional: counterevidence is a real
join node (the investigate renderer prints the section even when empty —
anti-anchoring), and the only `text`-emitting node is `code.context`
(and `evidence.diff`/`compare`, terminal by construction), so
bytes materialize exactly once, terminally (the DIGEST-CLOSURE law).

## No natural-language parser (the deliberate omission)

The proposal wanted English in, keyword-matched to an intent. We rejected
NL classification as the *primary* path, for a measured reason: the
product's own doctrine is **compact model-authored intent** — an agent
that serializes its intent into prose so a matcher can guess it back has
paid a lossy round trip. So:

- **Intent is a flag.** `--intent locate|impact|diagnose`. Missing → a
  teaching error that *suggests* one (`your question looks like --intent
  diagnose (advisory — nothing was run)`) and stops. It never guesses and
  executes.
- **Subject is a flag or the one unambiguous case.** `--symbol X`, or the
  question's sole identifier-shaped token (dotted / snake_case / CamelCase
  with an internal capital — "Where" and "What" are capitalized English,
  not subjects). Exactly one candidate → inferred **and disclosed**; zero
  or many → a teaching error naming the candidates. `infer_symbol` is
  pure and total.
- **The interpretation always shows.** The disclosure (`intent:` /
  `subject:` / `run:`) rides *above* the digest, never behind `--trace`.

This makes `ask`'s value proposition **routing** (which preset, correctly
slotted) rather than **compression** — and routing is honest for an
agent, while NL-in is a genuine win for a human at a terminal. The §6
referee must measure both audiences separately.

## The thin ops (Phase 0 — the reusable seams)

Three observe-class plan ops, useful to `ctx plan`/`investigate`
immediately, independent of `ask`:

- **`evidence.failures`** — failure census from *captured* facts, never a
  rerun. `run` absent → the latest derived run. Freshness against the
  current generation is computed and **declared**: a census whose facts
  were stamped at `gen:A` while the worktree is now `gen:B` carries
  `fresh: false` and a note proposing (never running) a refresh. This is
  the observe invariant made legible — the same generation semantics as
  the rest of the system (`generation_hash`: porcelain bytes + untracked
  size/mtime; a tracked-modified file's generation is its status line, so
  editing an already-dirty file correctly does not move it).
- **`code.symbols`** — structured symbol rows (identity · kind · range ·
  span) from skeleton-derived facts; census before detail, no outline
  text. An input warms facts for exactly those files (content-keyed, so
  unchanged files cost nothing).
- **`code.context`** — terminal bounded materialization: sites get
  line ± context, symbols get their clamped range. Emits `text`; by the
  closure law nothing downstream can lift bytes back into a
  representation. This is *the* refinement boundary at the plan tier.

Optional-input `code.refs/callers/callees/impact` (the proposal's §7
"add" item) already shipped in M-J — a source op accepts an input as a
capped `foreach` feed (`plan_ir.py`). Nothing to build.

## Verified end to end

On a seeded regression (a `raise` inside a changed function, its failing
pytest run captured under the birth gate), `ctx ask "Why is test_build
failing" --intent diagnose` returns, with **no test rerun**:

```
conclusion candidates (census): 1
  1. build · repo:cache.py:L2 · 1 test(s) · ValueError · planes dynamic+temporal+static
     tests: test_cache.py::test_build
counterevidence:
  none found (1 probe(s) executed)
coverage:
  fails · evidence.failures · engine facts.sqlite · 1 rows · run:57a0f05c795c
  culprits · evidence.join · engine facts.sqlite · 1 rows
  …
```

One command read captured failures, joined them against the change set,
and named the culprit symbol with plane attribution — the round-1 haiku
scenario that once burned eight turns, in one digest.

## What was cut, and why (the audit's teeth)

A design review earns its keep by what it declines. From the proposal:

| Proposed | Disposition | Reason |
|---|---|---|
| NL parser as the primary path | **Cut** → flag + one unambiguous inference | lossy round trip for agents; misroutes its own examples ("How does … reach" = trace, not explain). Keep as optional sugar later, over presets. |
| `ctx reveal` / `ctx audit` new verbs | **Cut** | one line each, unspecified; the four-verb rebrand is a CLI migration nobody scoped |
| Rebrand run/ask to the whole public surface | **Deferred** | `ask` ships as a compiler *in front of* the existing verbs; the rebrand is its own versioned decision |
| `view:` handles, `ask show`, role projections | **Deferred** | the investigate digest already indexes addressable node blobs; projections need a measured multi-worker use, not a speculative noun |
| The §4 entity/relation/operation ontology (11 · 14 · 11) | **Cut** | no predicates behind `taints`/`co_fails_with`/`verified_by`; the shipped op names in the intent presets are sufficient and real |
| verify / review / trace / compare intents | **Shipped** (v0.29.0) | trace/compare observe-class; verify/review execute-class with the bounded-tier rejection enforced by the plan validator |
| Anticipatory inlining | **Deferred to shadow** | promote only from measured exact follow-ups (the house Wilson-gate idiom), never on a hunch |

The keep list underlines the two things better than the average version
of this idea: the ship gate is measured **per intent** ("do not hide a
weak intent behind an aggregate win"), and interactive exploration stays
as the long-tail escape hatch — contracts are a fast path, not a
mandatory ontology.

## Acceptance & the open referee

Shipped gates (v0.27.0): compiler determinism (same slots → same plan
bytes); teaching errors that suggest but never guess-and-run; the
no-rerun invariant (`diagnose` plans contain no `test.run`, proven at
compile time and end to end); freshness declared on stale facts;
`code.context` terminal by the closure law. `tests/test_ask.py`.

Open, deliberately: the §6 A/B/C referee — ordinary exploration (A) vs
`ctx q`/`get` (B) vs `ctx ask` (C) — gated at *retrieval turns ≤ 50% of
B, task success ≥ B, decisive recall ≥ B, unresolved omissions = 0,
low-complexity overhead ≤ 5%*, **measured per intent**. Presets-before-
parser gives that referee its C arm now; it runs with the next eval wave.

## Sequencing (M-L)

```
now  ──► Phase 0 thin ops (evidence.failures · code.symbols · code.context) ✅
         Phase 1 intent presets + ctx ask (locate · impact · diagnose) ✅
         Phase 2 trace · compare (observe) ✅  ·  Phase 3 verify · review (execute) ✅
next ──► the A/B/C referee, per intent (retrieval turns ≤ 50% of ctx q/get) ─┐
         NL compiler as sugar over presets (misclassification telemetry) ┤
         role projections + view manifest (after a measured multi-worker need)
later ─► shadow anticipatory prefetch (Wilson-gated, like every other promotion)
```

**Governing rule, kept from the proposal's best instinct:** exact facts
and indexes precede structural, textual, and neural retrieval; retrieve
relationships, not chunks; return candidates, counterevidence, coverage,
and addresses; materialize bytes only at the end; and reuse the shipped
executor, cache, and evidence layer rather than growing a second of
anything.
