# Routing — spending the right model on each part of a task

**Status:** current behaviour · **Receipts:**
[iterative orchestrator](../evals/iterative-orchestrator-2026-07-25.md) ·
[Gemini tier A/B](../evals/antigravity-3.6-flash-vs-3.5-flash-lite-2026-07-25.md)

> **New here?** `ctx` is straitjacket's command — the project is straitjacket,
> the binary is `ctx`. If `ctx run`, `ctx wrap` or "digest" are unfamiliar, read
> [How it works](HOW-IT-WORKS.md) first: ten minutes, one command walked through
> the whole system.

Not every part of a task needs your strongest model. `ctx orchestrate` splits one
task across the agent CLIs you have installed and pays for each part at the level
it actually needs.

> **Not to be confused with** [`routing-policy`](../plugins/antigravity/skills/ctx-harness/references/routing-policy.md),
> which is about *containment* — when to send a command through `ctx run`. This
> page is about **which model does which subtask**.

## What `ctx orchestrate` does

```bash
ctx orchestrate "add a caching layer" --dry-run   # plan and price it, launch nothing
ctx orchestrate "add a caching layer"             # run it
```

Four steps:

1. **Coordinate.** The cheapest installed harness reads the routing contract and
   emits a small dependency graph — a handful of nodes, each with a capability
   requirement and its dependencies.
2. **Assign.** Each node goes to the cheapest `(harness, model)` pair that clears
   its capability bar.
3. **Price and show.** The graph is validated, priced, and printed *before* any
   spend.
4. **Run.** Ready nodes run in parallel waves. Each dependent sees only its
   upstreams' `checkpoint:` digest — addressed evidence, never raw bytes.

A typical plan:

```
routing (4 nodes, 4 waves):
  explore    → claude/claude-haiku-4.5   (economy)
  plan       → codex/gpt-5.6-sol          (frontier) ⇐ explore
  implement  → codex/gpt-5.6-terra        (standard) ⇐ plan
  verify     → claude/claude-haiku-4.5   (economy)  ⇐ implement
```

Before paying for coordination, a high-confidence ordinary request compiles
directly to the smallest deterministic route that can complete it. The same
compiler is the fallback when a coordinator is unavailable or its plan is
rejected:

| Request shape | Fallback DAG |
|---|---|
| explain or inspect | `answer` |
| run a named test | `verify` |
| review a diff | `review` |
| explicitly small edit | `implement → verify` |
| explicit low-risk feature with named target and tests | `explore → implement → verify` |
| ambiguous or complex | `explore → plan → implement → verify` |

Classification uses anchored, whole-word request shapes. It never searches
arbitrary strings for fragments such as `test`; phrases containing `latest` or
`testimony` therefore cannot be mistaken for test requests. Mutations always
retain verification, and uncertainty always falls back to the full route. The
three-stage feature route requires all four signals: a named source target,
named acceptance tests, an explicit behavioral contract, and no architecture,
authorization, security, migration, schema, deployment, production, or breaking
scope marker. Its implementer is the live-proven Claude/Sonnet arm; missing or
underspecified signals keep the frontier planning turn.

Prices depend on the unattended hosts installed on the current machine and are
printed before execution. They are estimates, not claims about final wire cost
or task success. Simple routes also avoid a coordinator model turn; that avoided
call is not included in route-node estimates.

Note that the *cheap* model does the coordinating and the *expensive* one does
the thinking. Deciding who should do the work is much easier than doing it.

Every launch is claimed and handed back on the **task ledger**
([TASK-LEDGER.md](TASK-LEDGER.md)): a node that does not finish is classified
by *why* and handed to a deterministic steward, which retries a transient,
escalates a capability limit, leaves an incomplete contract for re-planning,
and stops honestly on a login failure or an exhausted budget — with the
decision on record before it is acted on. Budget is checked against what hosts
actually charged, and a claim the ledger cannot cover is refused before the
launch. A run the process did not survive resumes with
`ctx orchestrate --resume <task-id>`; finished nodes are restored, not re-run.

Automatic orchestration excludes hosts that cannot complete a one-shot run
unattended. Google's interactive `agy` CLI therefore remains available through
an explicit host pin, but it is not selected by normal assignment, escalation,
or coordination. `antigravity-sdk` remains the headless Gemini alternative. If
you explicitly pin `agy`, its lack of an output gate and flash-lite's measured
flood behavior still apply; see [Host capabilities](HOST-CAPABILITIES.md).

Every executed route appends a prompt-free receipt to
`.ctx-session-reads/route.jsonl`: task shape, selected host/model, estimated
tokens and spend, actual usage when the host exposes it, wall time, waves,
replans, exit status, and verification state. Actual usage comes only from
machine-readable host records: Claude's JSON result (including its reported
dollar cost), Codex JSONL turn usage, or the ctx-owned Antigravity SDK usage
record. Dollar cost is priced from measured tokens when the host does not
report dollars. Failed attempts, escalations, coordinator calls, and replans
all count. A receipt says `partial` or `unavailable` when any corresponding
host attempt lacks trustworthy usage; missing observations never become zero.

Process exit does **not** become a task-success claim. Explicit evidence labels
live separately in `route-labels.jsonl`; the two ledgers can be joined and
exported as a frozen AlphaEvolve replay corpus without task text, model output,
or checkpoint content.

## The unit of routing is the model

Each harness runs several models across tiers, so routing picks a
`(harness, model)` pair rather than a harness:

| harness | frontier | standard | economy |
|---|---|---|---|
| claude | claude-opus-4.8 | claude-sonnet-4.6 | claude-haiku-4.5 |
| codex | gpt-5.6-sol | gpt-5.6-terra | gpt-5.6-luna |
| antigravity | gemini-3.1-pro | gemini-3.6-flash | gemini-3.5-flash-lite |
| antigravity-sdk | gemini-3.1-pro | gemini-3.6-flash | gemini-3.5-flash-lite |

**Capability is a gate, not a score.** A node declares a minimum tier; models
below it are excluded outright rather than ranked lower. Among the survivors,
price is the default tie-break.

## Dimensions beyond price

Price and tier are coarse. They cannot express that one model greps instead of
dumping a 4,000-line log, or that the cheapest model per token turned out to be
the most expensive arm in a real build. `src/ctx/data/model-catalog.json` carries
the rest:

| dimension | what it is | status |
|---|---|---|
| `specialities` | work this model is good at | advisory |
| `anti_specialities` | declared poor fits | advisory |
| `latency_class` | `fast` / `moderate` / `deliberate` (a declared feel, not a measurement) | advisory |
| `throughput_output_tok_s` | **measured** output tokens/second | measured, 2 models |
| `observed_behaviour` | measured conduct from our own receipts | measured |
| `benchmarks` | sourced suite scores | **empty today** — see below |

**Be clear about what "advisory" means here.** The deterministic router gates on
capability tier and breaks ties on **price**; it does not yet read this catalog.
These dimensions inform *your* choice and the coordinating model's judgement —
`ctx.catalog` exposes them, `speciality_score()` scores a model against a node's
tags — but nothing in `hosts.pick_model` consults them today. Wiring them in
would change every existing cost estimate, so it has not been done quietly. Until
it is, treat the catalog as evidence for a pin, not as an automatic router input.

## The provenance rule

**Every quantitative claim in the catalog carries a `source`, and a claim without
one is not shipped.** `lint_catalog()` enforces it and
`tests/test_model_catalog.py` fails the build otherwise.

This is not bureaucracy. A routing decision made on an invented benchmark is
worse than one made on price alone, because it *looks* informed while being
fiction — and it silently steers every subsequent decision.

Two consequences you should reason with:

- **`benchmarks` maps ship empty**, so this dimension decides nothing today.
  Public scores for these model versions are
  not in this repo's evidence base. An invented SWE-bench number would be
  indistinguishable from a real one at the point of use, so none is shipped.
  Populate them yourself and they flow straight into routing.
- **Absent data means UNKNOWN, never bad.** A model nobody has measured scores
  *neutral*, not last. Unknown latency sorts as `moderate`, never optimistically
  as `fast` — the costly error is assuming a deliberate model is snappy.

`declared-heuristic` is a legitimate source value. It means a human made a
judgement call, and you are free to overrule it with a measurement.

## What we actually measured

These are this repo's own receipts, and they change routing more than any tier
label:

**Flood discipline splits by model, not by tier.** On a needle-in-a-log task
where the needle was greppable, `gemini-3.6-flash` worked out it should `grep`
and emitted **812 bytes of shell output for the entire task**. `gemini-3.5-flash-lite`
on the identical task ran the log dump **27 times — 7.8 MB, 1.5M tool-output
tokens**. Route flood-prone work to flash-lite only behind containment.

**The cheapest model per token was not the cheapest arm.** On a three-phase web
build, routing implementation to `gemini-3.6-flash` (a fraction of Sonnet's unit
price) came within $0.34 of the all-Claude arm — because the agent re-sent
**4.25M input tokens to produce 63k of output**. Weigh how much context each turn
drags behind it, not just the per-token price.

**Splitting plan from build cost nothing in quality.** Solo Opus and
Opus-plans/Sonnet-builds both scored 98% on the same three-phase task; the split
cost **$7.04 against $9.59**.

**Containment bounds the tail, not just the average.** Uncontained, the same task
with the same model swung **2.5× in billed tokens run to run** (162k → 414k
billed), because whether the agent floods once or seven times is decided at run
time. Contained, the same measure stayed in a 43.8k–80.8k band.

Those are *billed session totals*, not dollars — input and output are priced
differently, so the token ratio is not the cost ratio. Measured separately, the
**tool output entering context** went from 76k–531k tokens uncontained to a
353–569 token band contained. Two different quantities; do not read the 414k and
the 569 as before-and-after of the same thing.

Measured throughput today:

| model | output tok/s (median, n=8) |
|---|--:|
| `gemini-3.6-flash` | 91.3 |
| `gemini-3.5-flash-lite` | 58.8 |

Note the counter-intuitive part: **the cheaper economy model is the slower one**
here, by about 36%. `latency_class` (a declared feel about time-to-first-token)
and throughput (measured tokens/second) are different axes, and on this pair they
point in opposite directions. If you are routing fan-out work to flash-lite for
speed, this is the number that says otherwise.

The Claude models are deliberately absent: the only wall-clock data this repo has
for them mixes model time with browser-based grading, and publishing that as
throughput would be false precision.

## Pinning a host or model

Routing is automatic, but it is not a black box: any node can pin what it runs
on. A node in the graph accepts three optional fields beyond its capability
requirement:

| field | effect |
|---|---|
| `"host": "antigravity-sdk"` | run this node on that harness |
| `"model": "gemini-3.6-flash"` | run it on that model |
| `"prefer": "strong"` | among models that clear the tier, take the flagship rather than the cheapest |

`prefer: "strong"` is how the default pipeline sends planning to Opus instead of
the cheapest frontier model — a good plan is worth the strong model.

A pin that names an uninstalled host is ignored rather than obeyed, and the
router falls back to the cheapest model that clears the bar; `--dry-run` shows
you what was actually chosen, so check there rather than assuming a pin took.

**To bias routing across every task instead of one node**, edit the catalog
rather than pinning: adding `specialities` to a model makes it win the
speciality tie-break for that kind of work, and `anti_specialities` on its
competitors pushes them away. Both are advisory — they cannot beat the tier gate,
which is a hard exclusion, and tiers live in `src/ctx/hosts.py` rather than in
config.

## Tuning it for your repo

Drop a `.ctx-catalog.json` at your workspace root. It merges **per model**, so
you can adjust one without restating the table:

```json
{"models": [
  {"match": "gemini-3.6-flash",
   "latency_class": "deliberate",
   "latency_source": "measured on our runners, 2026-07"}
]}
```

Adding a benchmark:

```json
"benchmarks": {"swe-bench-verified": {"score": 0.71, "source": "https://…", "date": "2026-07"}}
```

A score without a `source` is rejected — deliberately.

Related: prices live in `src/ctx/data/model-prices.json` (override with
`.ctx-prices.json`); tiers and role coverage live in `src/ctx/hosts.py`.
`ctx wrap detect` prints the live join of all three.
