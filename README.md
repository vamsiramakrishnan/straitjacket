# straitjacket 🧥

**Status:** v0.19.0 (pre-1.0, minor bump per mechanism wave) · 330 tests · hosts: Claude Code + Antigravity · Apache-2.0

An artifact-backed, repository-aware context containment harness for coding
agents (Claude Code and Antigravity). Unbounded tool output becomes an
immutable artifact plus a bounded, deterministic, span-addressed digest —
the transcript stays an *index* over evidence, never a warehouse of it.

The operating thesis, proven wave by wave in `evals/`: **skills bias, hooks
bound, mechanisms measure.** Doctrine nudges the model, structural hooks
enforce budgets it can't forget, and a wire-level observer measures every
session so the next mechanism is built on receipts, not vibes. Every
omission — bytes, transcript blocks, or engineering decisions — keeps an
address (`run:`/span/elided-file/`ctx debt`).

## ⚡ Quickstart

```bash
pip install -e .            # runtime (stdlib-only core; extras optional)
ctx init                    # write ctx.toml + .ctxignore
ctx wrap claude --proxy -- -p "fix the failing tests"   # one harnessed session
ctx stats --session         # wire scorecard: rounds, cache, effort mix
ctx gain                    # cumulative containment savings
```

`ctx wrap` is ephemeral (hooks via --settings, zero residue); the
Antigravity plugin (`ctx antigravity install`) is the persistent form.
Opt-in extras: `--rescue-pct 70` (lossless mid-session rescue),
`[map]`/`[code]`/`[fast]` pip extras, `rg`/`ctags` binaries, and a Rust
post-hook accelerator (`native/ctx-hook-native`, ~3 ms vs Python's ~29 ms
startup floor — parity-tested byte-for-byte, never required).

## 🚪 The four gates

| Gate | Question it answers | Mechanisms (all shipped) |
|---|---|---|
| **1 · Birth** | can this output flood at the source? | `ctx run`/`seq`/`eval` capture, deterministic digests, lint/pytest/log profiles, anticipatory inlining, failure-asymmetric budgets |
| **2 · Entry** | what actually crosses the wire? | Tier-0 byte-exact observer proxy: `window.json`, `wire.jsonl` (usage, timing, tool census), scorecards |
| **3 · Residence** | what may stay, and for how long? | session read ledger, window-pressure loop, priced steering, epoch-latched lossless rescue, checkpoints |
| **4 · Emission** | what does the model put back? | emission governor tiers, cite-don't-quote, solution ladder + backward planning (each A/B-adopted), deliverable metrics |

## 🔒 The Core Invariant
> Every potentially unbounded operation MUST either execute inside straitjacket, returning a bounded artifact digest, or be flatly rejected before execution.

* **Zero Token Bloat** *(shipped)*: Multi-megabyte outputs are captured at the source. The model transcript functions as an index over repository state and artifacts, not a warehouse of raw payload bytes.
* **Absolute Determinism** *(shipped)*: Timings, temporary paths, ANSI noise, and locale differences are stripped from model-visible output; identical bytes yield byte-identical digests, keeping prompt-cache prefixes stable across sessions.
* **Transparent Steering** *(shipped)*: PreToolUse hooks silently rewrite flooding commands through `ctx run` (Claude Code and Antigravity dialects) — no denial round-trips, no standing prompt text.
* **Path Containment** *(shipped)*: repo-relative addressing with `..` and symlink-escape rejection; `ws:<alias>` roots for multi-workspace sessions.
* **Capability HMAC handles & isolated broker** *(planned, Phase 3)*: content-hash handles become unforgeable HMAC capabilities once the broker daemon owns the store under a separate OS identity. Until then, handles are content addresses scoped per workspace.

## ⚖️ Why straitjacket vs other context-saving approaches

| Approach | Failure mode straitjacket avoids |
|---|---|
| Post-hoc compaction / summarization | Rewrites history: loses evidence irrecoverably, invalidates the prompt-cache prefix, and summarizes *after* the tokens were already paid for once. straitjacket intercepts **before execution** — the raw bytes never enter the transcript. |
| RAG / vector memory | Probabilistic recall with no provenance. straitjacket retrieval is deterministic and coordinate-exact: `run:<id>#stdout --lines 8412:8422` returns the same bytes forever. |
| Middleware token trimming | Silent truncation drops the failing test at line 48,000. straitjacket digests report coverage explicitly and every omission has a continuation coordinate. |
| Advisory prompt rules ("keep output short") | The model forgets under pressure. The `PreToolUse` gate is structural: floods are denied with an executable remediation. |
| Prompt caching alone | Orthogonal — and straitjacket makes caching *work better*: append-only transcripts and byte-identical digests keep the cached prefix stable across turns and replays. |

Every artifact is also an audit trail: what ran, what it produced, and exactly which slices the model saw.

## 🥊 The stack, compared: straitjacket vs rtk, Headroom, Ponytail, Caveman

Named systems, mapped onto the four-gate taxonomy (birth → entry →
residence → emission; see ROADMAP.md). Studied and, where marked, measured
head-to-head this iteration (receipts in `evals/`).

| | **straitjacket** | **rtk** | **Headroom** | **Ponytail** | **Caveman** |
|---|---|---|---|---|---|
| Mechanism class | hooks + observer proxy + verbs + skill | Bash-hook filter binary | rewriting wire proxy | ruleset injection | prompting style |
| Gate 1 · Birth (at-source) | ✅ artifactize + deterministic digest | ✅ filter (lossy on success) | — | — | — |
| Gate 2 · Entry (wire) | ✅ byte-exact observer (Tier-0) | — | ⚠️ per-request rewriting (lossy) | — | — |
| Gate 3 · Residence (lifecycle) | ✅ read budgets, window pressure, epoch rescue, checkpoints | — | ⚠️ implicit (continuous compression) | — | — |
| Gate 4 · Emission (output + deliverable) | ✅ governor + discipline + solution ladder | — | — | ✅ ladder (advisory only) | ✅ terse narration (advisory, lossy) |
| Lossless with addresses | **always** (spans, elided-file stubs, debt ledger) | failures only (tee) | ❌ silent drops | n/a | ❌ |
| Deterministic | yes, spec'd + tested | mostly | no | n/a | n/a |
| Cache doctrine | prefix contract, epoch-latched rescue, scorecards | none | **anti**: measured 12–16 pt hit deficit, 3–6× write churn | none | none |
| Runtime measurement loop | wire scorecards → policy epochs → `gain` | `gain` analytics | dashboards | none | none |
| Enforcement | structural (hooks, budgets) | rewrite hook | proxy force | none (rules) | none |
| Host reach | 2 deep (Claude Code, Antigravity) | 15 | any client (wire) | 20+ | any |

**Measured head-to-heads and absorptions** (all 2026-07-18, `evals/`):

- **Headroom** — benchmarked 3-way on four scenarios behind our observer.
  Its cache churn confirmed at request level (hit 80.6–84.2% vs our
  96.5–98.1%); on the long task our mechanisms beat it outright (42 turns /
  243s vs 53 / 279s). Its one structural edge — rescuing an already-bloated
  transcript — was taken losslessly in v0.10.0 (epoch-latched elision:
  ~18× less cache churn than per-request rewriting, every elided byte
  file-backed and addressed). Still better than us at: zero-integration
  generality (any client, no workspace) and cross-session memory — out of
  scope here by principle.
- **rtk** — ideas stress-tested on real corpora rather than benchmarked
  head-to-head. Two hypothesis reversals followed: diagnostics needed
  *structure not compression* (→ `lint/v1` exact censuses + per-file
  spans; live lint-fix benchmark went honest-loss → iterate → parity), and
  our own scaffold was inflating small outputs (→ slim inline emission).
  Absorbed: failure-asymmetric budgets, `ctx gain`. Still better than us
  at: 100+ bespoke filters, single-binary <10ms packaging, 15-host reach.
  Structurally behind: lossy success paths, no native-tool (Read/Grep)
  coverage, no cache or measurement doctrine.
- **Ponytail** — its core idea (the solution ladder) A/B-tested on a live
  creation task and **adopted on evidence**: −28% turns, −33% time, −17%
  cost, 9% less product code with *more* test code. Shipped as discipline
  prompt + skill rule 13 + `ctx debt` (its debt index, rebuilt as our
  declared-omission principle applied to decisions). Still better than us
  at: 20-host reach via rule files. Structurally behind: advisory-only —
  no enforcement, no measurement of whether the ladder held (we measure
  it per session via deliverable scorecard metrics).
- **Caveman** — lossy telegraphic narration, absorbed in lossless form:
  cite-don't-quote with resolvable handles (skill rules 11–12) instead of
  compressed prose. Its quiet-needle-style failure mode (evidence
  destroyed to save tokens) is the exact anti-pattern our spans/stubs
  exist to prevent.
- **Maki** (maki.sh) — its sandboxed-interpreter collapse (a script chains
  N operations; intermediates never reach the transcript — their demo:
  1300× context reduction) absorbed by design as `ctx eval`, the computed
  generalization of `ctx seq`: the script is a content-addressed blob
  cited in the digest header, streams stay span-addressable, tracebacks
  are path-free (`File "<stdin>"`), sub-steps opt into per-step handles
  via nested `ctx run`. Not yet benchmarked head-to-head. Still better
  than us at: OS-level sandbox isolation (ours arrives with the broker,
  Phase 3), skeleton indexing across 15 languages (ours: priced outline +
  map, Python-deep only), model-tier subagent selection. Structurally
  behind: no provenance — script and output vanish into the chat log with
  no address.

**Regime scoreboard** (worst case and best case, all measured):

| Regime | straitjacket vs naive | vs the field |
|---|---|---|
| Catastrophic floods | 456 tok vs ~222k | Headroom silently dropped the needle (347,595→68) |
| Repo comprehension | only-correct-answers across rounds; first-ever haiku pass | untested by others |
| Long overhaul | −21% turns, −9% time, −16% output | beats Headroom on turns/time at par cost |
| Tiny surgical tasks | parity (was 4.5×; graduated engagement) | rtk-class tasks: parity is the ceiling |
| Mechanical bulk repair | parity after per-file-span iteration | our structurally worst regime, no longer a loss |

The through-line: every system above has one good idea held back by a
missing layer — rtk filters without addresses, Headroom rescues without
cache stability, Ponytail biases without measurement, Caveman compresses
without provenance. straitjacket's claim is not a better single trick; it
is that **skills bias, hooks bound, mechanisms measure** — one system
where each layer catches what the previous one can't guarantee, and every
omission (bytes, blocks, or decisions) keeps an address.

## 🏗️ Architectural Topology
straitjacket maps directly to Antigravity's extension architecture, supporting scaling tiers of enforcement:

```
╔════════════════════════════════════════════════════════════════════╗
║                    Antigravity Engine Context                      ║
║                                                                    ║
║  ┌──────────────────────────┐      ┌───────────────────────────┐  ║
║  │   ctx-harness skill      │      │   ctx-harness plugin      │  ║
║  │  (Protocol Training)     │      │  (MCP + PreToolUse Hooks) │  ║
║  └────────────┬─────────────┘      └──────────────┬────────────┘  ║
╠════════════════╪═══════════════════════════════════╪════════════════╣
                 │                                    │
                 ▼                                    ▼
╔════════════════════════════════════════════════════════════════════╗
║                      straitjacket Core                             ║
║                                                                    ║
║  ┌────────────────────────────────────────────────────────────┐   ║
║  │           ctx-core harness                                 │   ║
║  │  (Execution Scoping, CAS Persistence, Digest Generation)   │   ║
║  └────────────────────────────┬─────────────────────────────┘   ║
╠═══════════════════════════════╪═════════════════════════════════╣
                                 ▼
╔════════════════════════════════════════════════════════════════════╗
║                       Hardened Broker                              ║
║  (Isolated OS/Container Identity, Unix Socket, Encrypted Catalog)  ║
╚════════════════════════════════════════════════════════════════════╝
```

## 🧩 Deployment Strengths

| Mode | Integration | Guarantee | Status |
|---|---|---|---|
| Skill Mode | SKILL.md only | **Advisory**: Agent is trained on protocol discipline but can bypass it. | shipped |
| Plugin Mode | Skill + MCP + PreToolUse Hooks | **Enforced**: Intercepts recognized tool paths; transparent substitution steering; highly resistant to accidental bypass. | shipped |
| Native Harness | SDK Agent with raw built-ins stripped | **Structural**: Built-ins like run_command are removed; raw output cannot physically enter context. | planned (Phase 4) |
| Hardened Mode | Native Harness + Isolated Broker | **Isolation-Backed**: Broker runs under a separate OS identity/container; sandboxed shell cannot read the CAS database. | planned (Phase 3) |

## 🧭 Selector Grammar
Absolute host paths never appear in model-visible output. The shipped
reference grammar has two address spaces:

* **Repository selectors** (live workspace state, snapshot-on-read):
  * `repo:` — the active workspace root
  * `repo:src/payments/service.py` — a current file
  * `repo:services/payments` — a scoped subtree
  * `ws:api/repo:src/main.py` — explicit root in multi-workspace sessions
  * `--scope payments` — named monorepo scopes from committed `ctx.toml`
* **Immutable artifact handles** (content-addressed, workspace-scoped):
  * `run:7bd91f2a4c3d` / `run:7bd91f2a4c3d#stdout` — captured invocation and its exact streams
  * `snapshot:fe21c91ad4e8` — file state pinned at read time
  * `blob:…`, `checkpoint:…` — raw content and frozen task epochs

*(Planned, Phase 3: handles upgraded to project-scoped HMAC capabilities once
the isolated broker owns the store.)*

## 🛠️ The Unified Verbs

Full surface at a glance (details for the core verbs below; flags in
`plugins/antigravity/skills/ctx-harness/references/verbs.md`):

| Verb | One line |
|---|---|
| `run` / `seq` | birth-gate capture; `seq` runs a declared N-step tree in one round, each step addressable |
| `eval` | programmable capture: a Python script chains N ops with computed control flow in one round; only its digest returns, and the script itself is an addressable `blob:` (the Maki absorption) |
| `search` / `get` / `stats` | batched patterns · exact slices (`--lines/--span/--symbol/...`) · shape stats, or a **priced symbol outline** on a single code file |
| `map` / `def` / `refs` / `diag` | ranked priced codebase map · symbol definition/reference/diagnostic verbs |
| `diff run:A run:B` | regression delta between captured runs, span-backed |
| `stats --session` / `gain` | wire scorecard (rounds, cache classes, effort mix) · cumulative savings |
| `checkpoint` / `pin` / `gc` | cache epochs · retention leases · mark-and-sweep |
| `debt` | declared-omission ledger for deferred engineering decisions |
| `wrap` / `proxy` / `hook` | session harness · Tier-0 observer (opt-in Tier-1 `--rescue-pct`) · host hook stages |

The Model-facing MCP layer is frozen into one stable tool surface (**ctx**), handling operations entirely via parameter states instead of dynamic tool injection.

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
so the user's permission flow remains visible (SPEC §10.4).

### 1. run
Executes an arbitrary process argv directly (no ambient shell unless `--shell`) inside the workspace. Captures stdout and stderr into distinct immutable blobs, records exit codes, signals, and timeouts, and returns a bounded deterministic digest — or the complete output verbatim when it fits the budget (zero-hop inline).

```bash
ctx run --focus "find test failures" --cwd services/payments -- pytest -q
```

### 2. search
Multi-pattern queries over live files or captured artifacts. Uses ripgrep when installed (transparent Python fallback), respects `.gitignore` + `.ctxignore`, snapshots returned repo evidence, and reports scanned coverage, match count, and truncation.

```bash
ctx search repo: 'TimeoutError' 'deadline' --glob '**/*.py' --context 3
ctx search run:7bd91f2a4c3d 'risk-api' --context 3
```

### 3. get
Exact, bounded slice of a repository file or artifact by lines, bytes, JSONL records, JSON pointer, or Python symbol.

```bash
ctx get run:7bd91f2a4c3d#stdout --lines 8412:8440
ctx get repo:svc/retry.py --symbol Handler.process
ctx get run:7bd91f2a4c3d#stdout --span e37f99e4a5   # token minted in the digest
```

Digests attach deterministic **span tokens** at every omission point
(template groups, failure blocks). Resolving a span is always bounded:
small regions return exact lines, large regions return a zoom sub-digest
that mints further sub-spans — retrieval structurally cannot re-flood the
transcript, and tokens are content-derived (replayable, leased, no TTL).

Oversized requests return a bounded preview plus continuation coordinates instead of silently dropping chunks.

### 4. stats
Exposes high-level metadata maps detailing repository layouts, tree sizes, languages, dirty git state parameters, or internal artifact shapes without leaking raw file context.

```bash
ctx stats repo: --scope payments
ctx stats run:7bd91f2a4c3d
```

### 5. map
Deterministic, budget-fitted codebase map: files ranked by a reference graph
(imports + symbol usage, evidence-weighted — files implicated in recent
captured runs rank hotter), top symbols per file, every entry addressable via
`repo:file --symbol X`. Uses grimp+networkx when the `[map]` extra is
installed and universal-ctags opportunistically for non-Python; transparent
builtin fallback otherwise. The active engine is disclosed in the map header
(`engine grimp+networkx` / `engine builtin`) and cache key; identical
worktrees yield byte-identical maps.

```bash
ctx map --budget 500 --focus payments
```

### 6. diff
Run-to-run regression digest between two captured invocations: exit/signal
and stream-size deltas, test failure-set deltas with traceback coordinates,
log template deltas (new-in-B templates carry minted spans), and a `next:`
line pointing at the most salient new evidence.

```bash
ctx diff run:7bd91f2a4c3d run:9ae02c17b5ff
```

## 💾 Digest Anatomy

Real output of `ctx run` on a 20,001-line operational log (`logtemplate/v1`,
the deterministic Drain-style template miner):

```
[ctx run:51c70b74fa1f profile=logtemplate/v1]
cwd: .
command: python3 emit.py
exit: 0
stdout: 20,001 lines · 1.2 MiB · est 304,113 tokens
stderr: 0 lines · 0 B
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

~304k tokens → ~210 model-visible tokens; the structurally anomalous line
survives verbatim with an exact retrieval coordinate. Profiles ship for
text, JSON, JSONL, logs, pytest, go test, jest/vitest, compilers/linters,
and git diffs. Small outputs skip digesting entirely and return whole
(zero-hop inline).

## 🛡️ PreToolUse Gate Policy

The shipped classifier is conservative and config-driven (shlex + wrapper
unwrapping + bounded-chain analysis — not a full shell AST; that remains the
Phase 3+ hardening goal). Under default `steering = "auto"` it **rewrites
instead of denying**:

* **Untouched**: ctx-routed calls, bounded commands and all-bounded chains (`pwd`, `git status --short`, `which ctx; ls`), small file reads, `cmd > file 2>&1` redirections to real files.
* **Silently rewritten**: framework suites, raw `cat`/`find`/`git diff`, unbounded package/cloud commands → routed through `ctx run`; oversized reads → bounded `limit` reads; single-file grep → match-capped.
* **Forced confirmation (never rewritten)**: secret-bearing paths, outside-workspace access, interactive programs.

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
  "updatedInput": {"command": "ctx run -- pytest -q"},
  "permissionDecisionReason": "CTX_CONTEXT_GUARD: routed through ctx for bounded capture"}}
```

Strict installs set `steering = "deny"` to keep the pure
deny-with-remediation contract. Fail-open on internal error is the default;
fail-closed is one config line.

**Cumulative read-budget governor**: beyond per-command classification, every
allowed native file read charges a per-session byte ledger. Past
`[budgets] session_read_budget_bytes` (default 256 KiB) reads come under
graduated pressure — bounded limit-window rewrites under auto steering,
deny-with-remediation under strict — teaching targeted alternatives
(`ctx search`, `--symbol`, `--lines`). Below budget, behavior is
byte-identical to the ungoverned contract; the ledger fails open on any IO
error.

## 🕵️ Auditable delegation: ctx-explorer

Sub-agent quarantine with provenance: the shipped `ctx-explorer` agent
definition (installed ephemerally by `ctx wrap claude`, persistently by the
Antigravity plugin) instructs forked explorers to gather evidence via ctx
verbs and report in checkpoint shape — conclusion, evidence handles with
coordinates, searches attempted including negative ones. Fork tool calls flow
through the same PreToolUse steering, so their evidence lands in the shared
artifact store and every claim in the report resolves via `ctx get`; a claim
without a handle must be labeled a hypothesis.

## 📊 Measured results (2026-07-17, details in `evals/`)

* End-to-end debugging task under Claude Code, matched warm caches, N=5 per
  arm: **cost parity within run variance** — harnessed median $0.115 / 6.8
  turns vs naive $0.098 / 6.2 turns, 5/5 correct in both arms, **zero denial
  round-trips**. Harness overhead on a small clean task: ~13% (~$0.01), down
  ~15x from the v0.1 deny-mode design — while adding provenance, budgets,
  redaction, and flood immunity naive has none of.
* Needle-drop vs Headroom 0.32.0 on a 20k-line log: loud (ERROR) needle —
  both preserve it; **quiet structural needle — Headroom silently drops it
  (347,595 → 68 tokens, no trace), logtemplate/v1 preserves it verbatim with
  its coordinate**.
* Repo-overhaul rematch on v0.6: **harnessed arm 40% cheaper than naive
  ($2.21 vs $3.70) and faster (6.1 vs 7.2 min) at quality parity**.
* 2026-07-18 mechanism waves (scenario matrix, cache-economics study,
  Headroom 3-way, lint-fix rounds, ladder A/B, live rescue validation):
  see the regime scoreboard above and
  [`evals/matrix-2026-07-18.md`](evals/matrix-2026-07-18.md) ·
  [`evals/rtk-corpus-2026-07-18.md`](evals/rtk-corpus-2026-07-18.md) ·
  [`docs/LOSSLESS-RESCUE.md`](docs/LOSSLESS-RESCUE.md) ·
  [`docs/PRICED-CONTEXT.md`](docs/PRICED-CONTEXT.md).

### Evals

* [`evals/ab-claude-code-2026-07-17.md`](evals/ab-claude-code-2026-07-17.md)
  — harnessed vs naive Claude Code on a buried-evidence debugging task:
  487× first-exposure token reduction; N=5 batch shows cost parity (~13%
  overhead) at 5/5 correctness in both arms, zero denial round-trips.
* [`evals/headroom-needle-drop-2026-07-17.md`](evals/headroom-needle-drop-2026-07-17.md)
  — quiet-needle head-to-head vs Headroom 0.32.0: their needle-drop rate
  100%, ours 0%, because rarity is structural, not lexical.
* [`evals/overhaul-3arm-2026-07-17.md`](evals/overhaul-3arm-2026-07-17.md)
  — three-arm repo-overhaul benchmark (naive / straitjacket / Headroom): no
  quality degradation from context mediation; the v0.6 rematch flips the
  cost sign to **−40% vs naive**.

## 📂 Source Code Layout

```
straitjacket/
├── src/ctx/
│   ├── cli.py               # Lazy-dispatch CLI; hook fast path bypasses argparse
│   ├── hook.py              # PreToolUse classifier (stdlib-only hot path, ~40ms)
│   ├── mcp.py               # Bounded MCP stdio server (single `ctx` tool, op discriminator)
│   ├── workspace.py         # Resolution order, identity, path confinement
│   ├── store.py             # CAS blobs, SQLite WAL catalog, leases, gc
│   ├── execution.py         # Birth-time capture runner (spooled, never in memory)
│   ├── refs.py              # run:/blob:/snapshot:/repo:/ws: reference grammar
│   ├── retrieval.py         # search / get / stats with budgets + continuations
│   ├── repomap.py           # ctx map: ranked, budget-fitted codebase map
│   ├── rundiff.py           # ctx diff: run-to-run regression digests
│   ├── checkpoint.py        # ctx checkpoint: pinned task-epoch manifests
│   ├── wrap.py              # ctx wrap: one-command harnessed sessions
│   ├── proxy.py             # ctx proxy: Tier-0 pass-through wire observer
│   ├── textutil.py          # ANSI stripping, deterministic redaction, budgets
│   ├── config.py            # ctx.toml policy loading
│   ├── installer.py         # Plugin rendering, init, doctor
│   └── digest/              # Deterministic profiles: text, json, jsonl, pytest, logs, builds
├── plugins/antigravity/     # Plugin template: plugin.json, hooks.json, mcp_config.json, skills, agents (ctx-explorer)
├── spec/                    # Normative SPEC, acceptance suite, ADRs, wire schemas
└── tests/                   # Acceptance-oriented determinism & security suite
```

## 🚀 Setup & Installation

Dependency policy is tiered by path criticality:

- **Hook hot path** (runs on every tool call): stdlib-only, ~40ms cold start.
- **Runtime**: one small pure-Python dep (`pathspec`) for true gitignore
  semantics in `.ctxignore` matching.
- **Opportunistic**: if **ripgrep** is on PATH, `repo:` searches use it
  (SIMD prefilter, parallel walk, no per-file Python reads) and fall back to
  the built-in engine transparently — same output contract, same coordinates.
- **Dev-only**: `jsonschema` validates manifests against the vendored wire
  schemas in tests and `ctx doctor`.

```bash
# Install the runtime once.
uv tool install ctx-harness      # or: pip install -e .

# Render the repo-scoped plugin (absolute executable paths baked in).
ctx antigravity install --scope workspace --workspace .

# Write committed policy (ctx.toml) and capture exclusions (.ctxignore).
ctx init
```

The installer renders into `<repo>/.agents/plugins/ctx-harness/` with the
skill embedded — one installation activates all surfaces. It refuses to
install alongside a standalone `.agents/skills/ctx-harness` (SPEC §4.3).

### Instant wrap

One command puts a supported agent session under the harness with nothing to configure:

```bash
ctx wrap claude -- -p "fix the failing test"   # ephemeral: hooks injected via --settings, removed on exit
ctx wrap antigravity                           # persistent: renders the workspace plugin
```

The Claude wrap leaves zero residue (the hook settings live in a temp file for the session's lifetime), while the Antigravity wrap is a persistent workspace install. Use `ctx wrap --print-config <host>` to print the equivalent configuration for CI or manual setup.

### Wire observer (Tier 0)

`ctx wrap claude --proxy` additionally routes the session's Anthropic API
traffic through `ctx proxy`, a localhost-only pass-through observer:
byte-exact relay (SSE unbuffered) plus a fail-open observation tap recording
provider-reported usage and context-window fullness (`window.json`) and a
per-exchange block census with tool_result sizes (`wire.jsonl`) — no request
bodies, no auth headers. `ANTHROPIC_BASE_URL` is injected only into the child
process; if the proxy fails to start, the session continues unproxied.

### Repository Configuration
Commit a `ctx.toml` at the workspace root:

```toml
version = 1

[budgets]
digest_tokens = 480
result_tokens = 1200
turn_retrieval_tokens = 2800
max_inline_bytes = 16384

[guard]
mode = "guarded"               # advisory | guarded | strict
unknown_command = "force_ask"
internal_error = "allow"       # fail-open: a broken guard must not brick the workspace
```

### Operational Checkup
Verify hook integrity, plugin manifests, store access, and classifier behavior:

```bash
ctx doctor --antigravity
```

### Development

```bash
pip install -e '.[dev]'
pytest        # acceptance-oriented suite: determinism, budgets, hook contract, escapes
```
