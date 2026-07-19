<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/hero.svg">
  <img src="assets/readme/hero-light.svg" width="100%" alt="straitjacket — context containment harness for coding agents. A 304,113-token log becomes a ~210-token digest, and the one anomalous line keeps an exact retrieval address.">
</picture>

[![Tests](https://github.com/vamsiramakrishnan/straitjacket/actions/workflows/ci.yml/badge.svg)](https://github.com/vamsiramakrishnan/straitjacket/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/github/license/vamsiramakrishnan/straitjacket)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-architecture-blue)](docs/README.md)

[Quickstart](#-quickstart) · [The four gates](#-the-four-gates) · [Digest anatomy](#-digest-anatomy) · [Comparisons](#-comparisons) · [Design docs](docs/README.md) · [Roadmap](ROADMAP.md)

**Status:** v0.24.0 (pre-1.0, minor bump per mechanism) · 733 tests · hosts: Claude Code + Antigravity · Apache-2.0

</div>

One `pytest -q` can dump 300k tokens into your agent's transcript. Every
turn after that re-sends them, so you pay for those tokens again on every
round — a routine `mcp__github__list_commits` alone is ~19.8k tokens per
round. Then compaction deletes the one line you needed, with no trace it
ever existed.

straitjacket stops that at the source. The raw bytes go into an immutable
local store, and the transcript gets a small, deterministic digest instead.
The digest is a fixed size no matter how much output the command produced.
Every byte it leaves out keeps an address, so you can pull the exact
original bytes back at any later turn.

You run coding agents daily and pay per token per turn. This keeps the
window small, keeps the cost down, and keeps the failing test line
retrievable after compaction would have dropped it.

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/flow.svg">
  <img src="assets/readme/diagrams/flow-light.svg" width="100%" alt="Tool output hits the birth gate; every raw byte lands in an immutable artifact store; the transcript gets a bounded, span-addressed digest; ctx get returns the exact bytes any time.">
</picture>

</div>

Raw bytes stop at the gate. The transcript carries the digest and its
addresses. Any address resolves back to the exact original bytes later.

## ⚡ Quickstart

```bash
pip install -e .            # runtime (stdlib-only core; extras optional)
ctx init                    # write ctx.toml + .ctxignore
ctx wrap claude --proxy -- -p "fix the failing tests"   # one harnessed session
ctx stats --session         # wire scorecard: rounds, cache classes, effort mix
ctx gain                    # cumulative containment savings, by verb
```

If you run **Claude Code**: `ctx wrap claude --proxy -- -p "..."` runs one
harnessed session. Hooks are injected via `--settings` for that run only and
removed when it ends, so nothing is left behind in your config.

If you run **Antigravity**: `ctx antigravity install` installs the plugin
once, and the harness stays on for every session.

Both get the same capture, the same digests, and the same retrieval
addresses. Opt-in extras: `--rescue-pct 70` (lossless mid-session rescue),
`[map]`/`[code]`/`[fast]` pip extras, `rg`/`ctags` binaries, and a Rust
post-hook accelerator (`native/ctx-hook-native`, ~3 ms vs Python's ~29 ms
startup floor — parity-tested byte-for-byte, never required).

## 🆕 New in v0.19–0.20

- **Head/tail evidence windows.** CLIs print their conclusion at the END of
  the output. Large `text/v1` digests now show the first 5 and last 5 lines
  (configurable) with real line numbers; the omitted middle keeps a
  deterministic span and a `ctx get --lines` continuation. This came from a
  measured miss: a flood scenario's own SUMMARY line was being dropped.
- **Long-runner backgrounding.** `ctx run --bg-after 30 -- <cmd>`: finish
  within 30s and you get the normal digest, byte-identical to a foreground
  run. Outlive it and the transcript gets `job:<id>` immediately while output
  spools to the store. `ctx job <id>` is a bounded live tail, never a flood;
  finalized jobs are ordinary `run:` artifacts.
- **Programmable capture: `ctx eval`.** One Python script chains N operations
  with computed control flow; only its bounded digest returns, and the script
  itself is stored as an addressable `blob:` cited in the digest header.
  Measured: 146 tokens vs 96k naive on a 30-file aggregate
  ([`evals/eval-collapse-2026-07-18.md`](evals/eval-collapse-2026-07-18.md)).
- **Adoption-measured steering.** The hook detects eval opportunities (python
  heredocs, `-c`, ephemeral scripts), suggests the collapse at that point,
  and records every opportunity — so adoption is a measured ratio per
  session, not a guess.

Full history: [`CHANGELOG.md`](CHANGELOG.md).

## 🔒 The core invariant

> Every potentially unbounded operation MUST either execute inside
> straitjacket, returning a bounded artifact digest, or be flatly rejected
> before execution.

- **Zero token bloat** *(shipped)*: multi-megabyte outputs are captured at
  the source; the transcript indexes repository state and artifacts instead
  of holding the payload bytes.
- **Absolute determinism** *(shipped)*: timings, temp paths, ANSI noise, and
  locale differences are stripped; identical bytes yield byte-identical
  digests, so your prompt-cache prefix stays stable across sessions.
- **Transparent steering** *(shipped)*: PreToolUse hooks rewrite flooding
  commands through `ctx run` in place — no denial round-trips, no standing
  prompt text.
- **Path containment** *(shipped)*: repo-relative addressing that rejects
  `..` and symlink escapes; `ws:<alias>` roots for multi-workspace sessions.
- **Capability HMAC handles + isolated broker** *(planned, Phase 3)*:
  content-hash handles become unforgeable capabilities once the broker daemon
  owns the store under a separate OS identity.

## 🚪 The four gates

A token passes four points in its life. One artifact store handles all four
as gates, and every shipped mechanism attaches to exactly one of them.

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/gates.svg">
  <img src="assets/readme/diagrams/gates-light.svg" width="100%" alt="The four gates. Birth: can it flood at the source. Entry: what crosses the wire. Residence: what may stay and for how long. Emission: what goes back out. One artifact store serves all four.">
</picture>

</div>

Birth prevents floods at the source, Entry observes what crosses the wire,
Residence controls what stays and for how long, and Emission governs what the
model writes back.

| Gate | Question | Mechanisms (all shipped) |
|---|---|---|
| **1 · Birth** | can this output flood at the source? | `ctx run`/`seq`/`eval` capture, supervised backgrounding (`--bg`/`job`), head/tail evidence windows, deterministic digest profiles (lint/pytest/log/search/…), anticipatory inlining, failure-asymmetric budgets |
| **2 · Entry** | what actually crosses the wire? | Tier-0 byte-exact observer proxy (`window.json`, `wire.jsonl`), shape-dispatched PostToolUse gate for every faucet (MCP, WebFetch, Task, …), scorecards |
| **3 · Residence** | what may stay, and for how long? | session read ledger, window-pressure loop, priced steering, epoch-latched lossless rescue, checkpoints |
| **4 · Emission** | what does the model put back? | emission governor tiers, cite-don't-quote, solution ladder + backward planning (each A/B-adopted), deliverable metrics |

Sub-agents inherit all four. The shipped `ctx-explorer` agent reports in
checkpoint shape — conclusion, evidence handles with coordinates, negative
searches included — and any claim without a handle is labeled a hypothesis.
Fork evidence lands in the shared store, and every claim resolves via
`ctx get`.

## 🪜 Choosing a verb: the capture ladder

The most common question — which verb do I use — as a flowchart:

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/ladder.svg">
  <img src="assets/readme/diagrams/ladder-light.svg" width="100%" alt="The capture ladder: native read for small bounded output; ctx run for one noisy command; ctx run --shell for pipe chains; ctx seq for N declared steps; ctx eval for computed control flow. Long work backgrounds into a job handle.">
</picture>

</div>

Use the lightest verb the work allows. Anything that outlives the wait
backgrounds into a `job:` handle instead of idling the session.

The measured differences
([`evals/eval-collapse-2026-07-18.md`](evals/eval-collapse-2026-07-18.md)):
a bash pipeline under `ctx run --shell` already collapses stream-shaped
chains (266 tok, one round). `ctx eval` wins on round count only when the
intermediate results are *structured*: the 30-file aggregate is 146 tok in
one round vs 96k naive, and a bounded-slice baseline cannot finish that task
at all. When a script fails mid-corpus, debugging is retrieval, not
re-execution: 299 tok to fix and rerun vs 192k to re-pay the raw chain.

### Long runners

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/longrun.svg">
  <img src="assets/readme/diagrams/longrun-light.svg" width="100%" alt="ctx run --bg-after 30: finish within 30 seconds and the digest is byte-identical to a foreground run; outlive it and the transcript gets job:<id> while output spools to the store. ctx job gives a bounded tail; finalized jobs become ordinary run: artifacts.">
</picture>

</div>

Don't idle on a long process (skill rule 15): background it, keep working,
collect the digest when you need it.

Six launch/kill/finalize races were identified and closed (single-writer
meta, idempotent finalization, orphan adoption). Job ids, pids, and
timestamps never enter content identity.

## 💾 Digest anatomy

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/containment.gif">
  <img src="assets/readme/containment-light.gif" width="100%" alt="Animated terminal: ctx run captures a 20,001-line flood streaming past; it collapses through the gate into a six-line logtemplate/v1 digest — 304,113 tokens become ~210 model-visible, and the needle line keeps an exact retrieval address.">
</picture>

<sub>The loop in six seconds: flood → gate → digest. (Editable static source: [`containment.svg`](assets/readme/containment.svg))</sub>

</div>

This is what one turn looks like with and without the harness:

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/econ.svg">
  <img src="assets/readme/diagrams/econ-light.svg" width="100%" alt="Without: one pytest run puts 304,113 tokens in the transcript, re-sent on every later turn until compaction deletes lines without trace. With: the transcript holds a ~210-token digest plus addresses; raw bytes stay on disk and the rest of the window is free.">
</picture>

</div>

Real output. First, the v0.20 head/tail window on a 4,809-line run with no
error keywords — the tail carries the conclusions, and the omitted middle
keeps an address:

```
[ctx run:ba3d1020ee8f profile=text/v1]
command: python3 emit2.py
exit: 0
stdout: 4,809 lines · 126.8 KiB · est 32,452 tokens
summary:
  head stdout:L1: processed item-0001 in 3ms
  head stdout:L2: processed item-0002 in 3ms
  ...
  … omitted stdout:L6-L4804 (4,799 lines) · span f40f9ab8c1
  tail stdout:L4807: p95 latency: 4ms
  tail stdout:L4808: slowest shard: catalog
  tail stdout:L4809: done at rev 8c1f
coverage:
  parsed: 4,809/4,809 lines
  shown: 10 spans · omitted: 4,799 lines
next:
  ctx get run:ba3d1020ee8f#stdout --lines 6:4804
```

Second, `logtemplate/v1` (deterministic Drain-style template mining) on a
20,001-line operational log:

```
[ctx run:51c70b74fa1f profile=logtemplate/v1]
command: python3 emit.py
exit: 0
stdout: 20,001 lines · 1.2 MiB · est 304,113 tokens
templates: 3 cover 20,001/20,001 lines
  19,999× L1: INFO worker-<*> checkout request req-<*> completed in budget
  1× L14238: INFO worker-<*> checkout request req-<*> fell back to legacy gateway after circuit opened
  1× L20001: RUN RESULT: all requests completed
exceptional:
  L14238: INFO worker-13 checkout request req-14237 fell back to legacy gateway after circuit opened
coverage:
  parsed: 20,001/20,001 lines
  shown: 5 spans · omitted: 19,996 lines
next:
  ctx get run:51c70b74fa1f#stdout --lines 14238:14241
```

~304k tokens become ~210 model-visible tokens, and the one anomalous line
survives verbatim with an exact retrieval coordinate, because it is selected
by structure, not by keyword. Profiles ship for text, JSON, JSONL, logs,
pytest, go test, jest/vitest, compilers/linters, search results, and git
diffs. Small outputs skip digesting and return whole (zero-hop inline, ~20
tokens of scaffold). Failing runs get 2× the digest budget of successes,
because a failure carries the evidence you need and a success rarely does.

Span resolution is bounded too: small regions return exact lines, large
regions return a zoom sub-digest that mints further sub-spans. Retrieval
cannot re-flood the transcript.

## 📐 The measurement loop

Every session produces wire-level ground truth about what it cost. The loop
turns that into committed steering policy, and `ctx gain` is your readout of
what it saved.

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/loop.svg">
  <img src="assets/readme/diagrams/loop-light.svg" width="100%" alt="The measurement loop: a wire observer measures every session; scorecards compile into committed policy epochs; the next session runs under tighter steering. ctx gain reports cumulative savings by verb.">
</picture>

</div>

Every branch a mechanism takes records what it did; those records compile
into the next epoch's policy.

Concretely:

- `ctx proxy` (Tier-0) relays Anthropic API traffic byte-exact and records
  provider-reported usage, window fullness, and a per-exchange block census —
  no request bodies, no auth headers. Fail-open: no proxy, no harm.
- `ctx stats --session` renders the scorecard: token classes, cache-hit
  breakdown (cold-prefix vs true invalidation vs suffix growth), ttfb vs
  generation, effort mix, deliverable metrics (LOC delta, files touched).
- The **prefix-stability contract** golden-hashes every injected prefix byte
  behind `PREFIX_VERSION`, because a 9-token prompt edit measurably cost one
  full cold cache rewrite per model (~56k tokens).
- A measured A/B is the bar for shipping a steering change: the solution
  ladder shipped only after −28% turns / −33% time / −17% cost; backward
  planning after −17% cost / −16% turns. The `ctx eval` adoption ledger
  exists because a live A/B showed the discipline winning while the verb went
  unadopted — recorded as debt, then instrumented.

## 🧾 Comparisons

### The field, in one table

Other tools in this space each do one thing well. We benchmarked or
stress-tested each, took the good idea without its cost, and recorded what
each still does better (all data in [`evals/`](evals/)).

| Approach | What it does well | Limitation (measured where marked) | How we took it |
|---|---|---|---|
| Post-hoc compaction / summarization | reclaim a bloated window | rewrites history; evidence irrecoverable, prefix cache invalidated | checkpoint-then-rescue: secure handles first, then clearing is lossless |
| RAG / vector memory | recall without resending | probabilistic, no provenance | deterministic addresses: `run:<id>#stdout --lines 8412:8422` returns the same bytes forever |
| **Headroom** (rewriting wire proxy) | rescue an already-bloated transcript | silent evidence drops (347,595→68 tok, no trace); cache hit 80.6–84.2% vs our 96.5–98.1%; 3–6× cache-write churn | v0.10 epoch-latched lossless rescue: ~18× less cache churn, every elided byte file-backed and addressed |
| **rtk** (bash-hook filter binary) | filter floods at the source | lossy on success paths; no addresses, no cache-stability policy | failure-asymmetric budgets, `ctx gain`, structure-not-compression `lint/v1` |
| **Ponytail** (ruleset injection) | the solution ladder | advisory only; never measured whether the ladder held | ladder A/B-adopted on evidence (−28% turns, −33% time, −17% cost) + `ctx debt` |
| **Caveman** (terse prompting style) | say less | destroys evidence to save tokens — the quiet-needle anti-pattern | cite-don't-quote with resolvable handles (skill rules 11–12) |
| **Maki** (sandboxed interpreter) | one script collapses N ops (their demo: 1300×) | no provenance: script and output vanish into the chat log | `ctx eval`: script is an addressable `blob:`, streams span-addressed, tracebacks path-free |

Headroom is the only one we ran head-to-head behind our own observer. On the
quiet structural needle it dropped the evidence 100% of the time (347,595 →
68 tokens, no trace) where `logtemplate/v1` dropped 0%. On the long task our
mechanisms beat it outright: 42 turns / 243s vs 53 / 279s at comparable cost.
The `ctx eval` live A/B ran four pairs: the one-script discipline won every
pair (−15–63% cost, fewer turns), but the verb itself went unadopted in bare
sessions, so we filed it as debt. The v0.20 teaching surface now detects,
suggests, and records every opportunity, and conversion is the next metric to
move. What each still does better than us, by design: Headroom's
zero-integration generality, rtk's 15-host reach and <10ms single binary,
Ponytail's 20-host rule files, Maki's OS-level sandbox (ours arrives with the
broker, Phase 3).

The needle case, drawn out — the same anomalous line under each approach:

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/fates.svg">
  <img src="assets/readme/diagrams/fates-light.svg" width="100%" alt="A 20,001-line log with one anomalous line. Compaction deletes it without trace. A rewriting proxy dropped it in every measured run (347,595 tokens in, 68 out). straitjacket's logtemplate profile kept it verbatim with an exact retrieval address.">
</picture>

</div>

### Regime scoreboard (worst case and best case, all measured)

| Regime | straitjacket vs naive | vs the field |
|---|---|---|
| Catastrophic floods | 456 tok vs ~222k first exposure (487×) | Headroom silently dropped the needle (347,595→68) |
| Repo comprehension | only-correct-answers across rounds; first-ever haiku pass | untested by others |
| Long overhaul | −21% turns, −9% time, −16% output | beats Headroom on turns/time at par cost |
| Tiny surgical tasks | parity (was 4.5×; graduated engagement fixed it) | rtk-class tasks: parity is the ceiling |
| Mechanical bulk repair | parity after per-file-span iteration | our worst regime, no longer a loss |
| Small spec-driven creation (haiku) | **current loss**: 33 turns (cap) vs naive's 11–26 at 2.7–3.8× cost; quality tied (16/16 holdout all arms), cache hit still best (96–98%) | diagnosed to one loop — pytest digest lacks the failing-test census — fix candidates ranked, referee frozen ([`evals/spec3-haiku-2026-07-18.md`](evals/spec3-haiku-2026-07-18.md)) |

Depth, per topic:
[`evals/matrix-2026-07-18.md`](evals/matrix-2026-07-18.md) (scenario matrix +
cache economics) ·
[`evals/headroom-needle-drop-2026-07-17.md`](evals/headroom-needle-drop-2026-07-17.md)
(needle-drop head-to-head) ·
[`evals/ab-claude-code-2026-07-17.md`](evals/ab-claude-code-2026-07-17.md)
(N=5 A/B: cost parity, 5/5 correct both arms, zero denials) ·
[`evals/overhaul-3arm-2026-07-17.md`](evals/overhaul-3arm-2026-07-17.md)
(v0.6 rematch: −40% cost vs naive at quality parity) ·
[`evals/rtk-corpus-2026-07-18.md`](evals/rtk-corpus-2026-07-18.md)
(real-corpus reversals + live lint-fix rounds) ·
[`evals/eval-collapse-2026-07-18.md`](evals/eval-collapse-2026-07-18.md)
(programmable capture) ·
[`docs/LOSSLESS-RESCUE.md`](docs/LOSSLESS-RESCUE.md) ·
[`docs/PRICED-CONTEXT.md`](docs/PRICED-CONTEXT.md) ·
[`docs/LADDERS.md`](docs/LADDERS.md) (the conditionality audit behind v0.20).

### What we took from each

- **rtk** → real corpora reversed our hypotheses before we built: diagnostics
  needed *structure, not compression* (`lint/v1` exact censuses; the live
  lint-fix benchmark went honest-loss → iterate → parity), and our own
  scaffold was inflating small outputs (slim inline: ~100–400 tok overhead →
  ~20).
- **Headroom** → its one structural edge (rescuing a bloated transcript) taken
  losslessly: epoch-latched elision, +$0.05 where per-request rewriting pays
  $0.90 in churn, 18 turns of lossless runway per 27k elided; live-validated
  with 10/10 facts correct including elided ones.
- **Ponytail** → solution ladder adopted only after the A/B won on every axis;
  rebuilt with enforcement (`ctx debt`) and per-session measurement.
- **Caveman** → terse narration kept, the loss dropped: citations resolve,
  compressed prose doesn't.
- **Maki** → the interpreter collapse generalized (`ctx seq` declared → `ctx
  eval` computed) with the provenance a raw sandbox drops.

## 🏗️ Architecture & deployment

```
skill (protocol)        plugin (MCP + hooks)
        │                        │
        └──────────┬─────────────┘
                   ▼
        ctx-core harness
        execution scoping · CAS store (SQLite WAL) · deterministic digests
                   │
                   ▼   (Phase 3)
        hardened broker — isolated OS identity, unix socket, encrypted catalog
```

| Mode | Integration | Guarantee | Status |
|---|---|---|---|
| Skill | SKILL.md only | **Advisory**: protocol-trained, bypassable | shipped |
| Plugin | skill + MCP + hooks | **Enforced**: transparent substitution steering on recognized tool paths | shipped |
| Native harness | SDK agent, raw built-ins stripped | **Structural**: raw output cannot physically enter context | planned (Phase 4) |
| Hardened | native + isolated broker | **Isolation-backed**: sandboxed shell cannot read the CAS database | planned (Phase 3) |

### Steering policy (the hooks)

The PreToolUse classifier is conservative and config-driven. Here is what
happens to a command you type:

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/lanes.svg">
  <img src="assets/readme/diagrams/lanes-light.svg" width="100%" alt="A PreToolUse classifier sorts every command into three lanes: bounded commands run untouched; flooding commands are rewritten through ctx run with the token price shown; secret paths, outside-workspace access and interactive programs always ask first.">
</picture>

</div>

Under default `steering = "auto"` it **rewrites instead of denying**:

- **Untouched**: ctx-routed calls, bounded commands and all-bounded chains,
  small reads, redirections to real files.
- **Silently rewritten**: framework suites, raw `cat`/`find`/`git diff`,
  unbounded package/cloud commands → `ctx run`; oversized reads → bounded
  limit windows; unbounded native `Grep` → capped with a pointer to the
  structured digest. Each rewrite reason carries the price: "~30k tok ≈ 15% of
  window" ([`docs/PRICED-CONTEXT.md`](docs/PRICED-CONTEXT.md)).
- **Forced confirmation, never rewritten**: secret-bearing paths,
  outside-workspace access, interactive programs.

Beyond per-command classification: a cumulative session read ledger puts
native reads under graduated pressure past 256 KiB, and the universal
PostToolUse gate replaces any tool result over 16 KiB — from any faucet, MCP
included — with a digest carrying a working `ctx get` ref, raw bytes
persisted losslessly first. Strict installs set `steering = "deny"`;
fail-open on internal error is the default, fail-closed is one config line.

### Source layout

```
straitjacket/
├── src/ctx/           # cli, hook (stdlib-only hot path), mcp, store (CAS+SQLite),
│                      # execution, refs, retrieval, repomap, rundiff, jobs, pyeval,
│                      # rescue, proxy, wrap, scorecard, digest/ (profiles)
├── native/ctx-hook-native/  # optional Rust post-hook shim (~3 ms), parity-tested
├── plugins/antigravity/     # plugin template: hooks, MCP config, skill, ctx-explorer agent
├── spec/              # normative SPEC, acceptance suite, ADRs, wire schemas
├── docs/              # design docs — EDC, reflex, ladders, priced context, rescue
├── evals/             # every measured claim in this README
├── assets/readme/     # README visuals (self-contained SVG, no remote fetches)
└── tests/             # 733 acceptance-oriented determinism & security tests
```

## 📖 Reference

### Verbs

Full flags and when-to-use detail:
[`plugins/antigravity/skills/ctx-harness/references/verbs.md`](plugins/antigravity/skills/ctx-harness/references/verbs.md).

| Verb | One line |
|---|---|
| `run` / `seq` | birth-gate capture; `seq` runs a declared N-step tree in one round, each step addressable |
| `eval` | programmable capture: a Python script chains N ops with computed control flow in one round; only its digest returns, and the script itself is an addressable `blob:` |
| `run --bg` / `--bg-after T` / `job` / `jobs` | long-runner backgrounding: `job:<id>` in the transcript, bounded live tail, `--wait`, `--kill`; finalized jobs are ordinary `run:` artifacts |
| `search` / `get` / `stats` | batched patterns · exact slices (`--lines/--span/--symbol/--records/--json-pointer/--bytes`) · shape stats, or a priced symbol outline on a single code file |
| `map` / `def` / `refs` / `diag` | ranked priced codebase map · symbol definition/reference/diagnostic verbs |
| `callers` / `callees` / `impact` | call graph: direct callers, callees, transitive blast radius (`--depth ≤6`) — one query replaces a recursive grep trace |
| `diff run:A run:B` | regression delta between captured runs, span-backed |
| `stats --session` / `gain` | wire scorecard (rounds, cache classes, effort mix) · cumulative savings |
| `checkpoint` / `pin` / `gc` | cache epochs · retention leases · mark-and-sweep |
| `debt` | declared-omission ledger for deferred engineering decisions (`add`/`list`/`resolve`) |
| `policy` | compiled steering policy from telemetry (`compile`/`show`) |
| `wrap` / `proxy` / `hook` | session harness · Tier-0 observer (opt-in Tier-1 `--rescue-pct`) · host hook stages |
| `init` / `doctor` | write `ctx.toml` + `.ctxignore` · validate hooks, manifests, store, classifier |

Examples:

```bash
ctx run --focus "find test failures" --cwd services/payments -- pytest -q
ctx run --bg-after 30 -- npm run build          # backgrounds if it outlives 30s
ctx seq 'pytest -q' 'ruff check .' 'npm run build'
ctx eval - <<'EOF'                              # computed control flow, one round
import json, glob, statistics
lat = [json.loads(l)["ms"] for f in glob.glob("runs/*.jsonl") for l in open(f)]
print(f"p95: {statistics.quantiles(lat, n=20)[18]:.0f}ms over {len(lat)} records")
EOF
ctx search repo: 'TimeoutError' 'deadline' --glob '**/*.py' --context 3
ctx get run:7bd91f2a4c3d#stdout --lines 8412:8440
ctx get run:7bd91f2a4c3d#stdout --span e37f99e4a5   # token minted in the digest
ctx get repo:svc/retry.py --symbol Handler.process
ctx stats repo:src/ctx/hook.py     # priced symbol outline: 12.8–54.5× cheaper than the file
ctx map --budget 500 --focus payments
ctx impact register_span --depth 4
ctx diff run:7bd91f2a4c3d run:9ae02c17b5ff
```

### Selector grammar

Every address has the same shape, and the same address returns the same
bytes on any later day:

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/diagrams/address.svg">
  <img src="assets/readme/diagrams/address-light.svg" width="100%" alt="ctx get run:51c70b74fa1f#stdout --lines 8412:8422 — the retrieval verb, an immutable content-addressed artifact id, the stream, and exact line coordinates. The same address returns the same bytes forever.">
</picture>

</div>

Absolute host paths never appear in model-visible output. Two address spaces:

- **Repository selectors** (live workspace state, snapshot-on-read): `repo:` ·
  `repo:src/payments/service.py` · `repo:services/payments` (subtree) ·
  `ws:api/repo:src/main.py` (multi-workspace) · `--scope payments` (named
  monorepo scopes from committed `ctx.toml`)
- **Immutable artifact handles** (content-addressed, workspace-scoped):
  `run:7bd91f2a4c3d` / `run:…#stdout` / `run:…#stderr` ·
  `snapshot:fe21c91ad4e8` (file state pinned at read time) · `blob:…` (raw
  content, incl. eval scripts) · `checkpoint:…` (frozen task epochs) · `job:…`
  (backgrounded runs, until finalized into `run:`)

*(Planned, Phase 3: handles upgraded to HMAC capabilities once the isolated
broker owns the store.)*

### MCP surface

One stable tool (**ctx**), operations selected by parameter — no dynamic tool
injection, so the prompt-cache prefix never churns:

```json
{
  "name": "ctx",
  "description": "Bounded retrieval against repository state or captured artifacts.",
  "input": {
    "op": "search | get | stats | map | repo | doctor",
    "ref": "run:<id>[#stdout|#stderr] | snapshot:<id> | repo:[path]",
    "patterns": ["TimeoutError", "deadline"],
    "selector": {"lines": "8412:8440"},
    "maxTokens": 1200
  }
}
```

Command execution stays on `ctx run` through the host's native command tool
so your permission flow stays visible (SPEC §10.4).

### Configuration

Commit a `ctx.toml` at the workspace root:

```toml
version = 1

[budgets]
digest_tokens = 480
result_tokens = 1200
turn_retrieval_tokens = 2800
max_inline_bytes = 16384
digest_head_lines = 5          # head/tail evidence windows (v0.20)
digest_tail_lines = 5
failure_budget_factor = 2.0    # failing runs get 2x the digest budget of successes

[guard]
mode = "guarded"               # advisory | guarded | strict
unknown_command = "force_ask"
internal_error = "allow"       # fail-open: a broken guard must not brick the workspace
```

Dependency policy is tiered by path criticality: the hook hot path is
stdlib-only; the runtime carries one pure-Python dep (`pathspec`);
`ripgrep`/`ctags`/`grimp`/`jedi`/`orjson` are opportunistic accelerators with
transparent fallbacks — same output contract, same coordinates, the active
engine disclosed in headers.

```bash
pip install -e .                            # from a clone (not yet on PyPI)
ctx antigravity install --scope workspace --workspace .   # persistent plugin
ctx wrap claude -- -p "fix the failing test"              # or: ephemeral wrap
ctx wrap --print-config claude              # equivalent config for CI/manual setup
ctx doctor --antigravity                    # verify hooks, manifests, store, classifier
```

`ctx wrap claude --proxy` also routes the session's Anthropic API traffic
through the localhost-only observer: byte-exact relay (SSE unbuffered),
fail-open tap recording usage and window fullness — no request bodies, no auth
headers. `ANTHROPIC_BASE_URL` is injected only into the child process; if the
proxy fails to start, the session continues unproxied.

Development:

```bash
pip install -e '.[dev]'
pytest        # 733 tests: determinism, budgets, hook contract, escapes
```

## 📚 Going deeper

[`docs/`](docs/README.md) — the design docs index: mechanism notes (priced
context, lossless rescue) and the current architecture work (EDC, reflex, the
composition algebra). [`spec/`](spec/) is normative; [`evals/`](evals/) holds
the measured data; [`CHANGELOG.md`](CHANGELOG.md) is the release history;
[`CONTRIBUTING.md`](CONTRIBUTING.md) explains the house rules for landing a
mechanism.

The same docs also build into a browsable site ([`site/`](site/), Astro +
Starlight): `cd site && npm install && npm run dev`, or deploy via the manual
[`docs-site` workflow](.github/workflows/docs-site.yml) once GitHub Pages is
enabled for the repo.

## 🗺️ Roadmap & license

[`ROADMAP.md`](ROADMAP.md) tracks what is next; the standing rule is to replace bytes with addresses. Next up is the broker era (Phase 3: isolated OS identity, HMAC
capability handles, warm LSP servers) and the conditionality audit's ranked
candidates ([`docs/LADDERS.md`](docs/LADDERS.md)): pressure-aware budgets
through a single resolver, hint follow-through telemetry, guard-mode outcome
accounting. Deliberately not planned: lossy pruning without addresses —
deleting bytes you cannot re-address is the failure mode this project exists
to prevent.

Apache-2.0.
