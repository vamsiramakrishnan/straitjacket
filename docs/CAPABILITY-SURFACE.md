<sub><a href="README.md">« straitjacket / docs</a></sub>

# Capability surface — the input side of containment

straitjacket has always contained the **output** side: unbounded tool output
becomes a bounded, addressable digest. The capability surface is the symmetric
**input** side — everything the host tells the model it *can do*, re-sent every
turn before any tool is called.

```
CAPABILITY CONTAINMENT   what the model is told it can do   (input)
EVIDENCE CONTAINMENT     what the model is shown after doing it   (output)
```

The governing law is one sentence:

> Do not place a capability, or its evidence, in context until the current
> task has earned the cost and the authority.

A 400-token MCP tool schema shown across 30 turns is 12,000 token-turns of tax
whether or not it is ever used. Tool count also degrades selection: a cheap
model with a clean 6-tool surface often beats a strong model drowning in 70
tools and 20k tokens of irrelevant instructions. Capability containment
attacks both.

## Preventive by default: bound *before* bloat

The point was never to measure bloat already in context — it was to stop it
entering. Two mechanisms make the audit preventive, mirroring the output side's
"capture before flood":

- **SessionStart gate.** A hook on all three hosts (Claude `SessionStart`,
  Codex `SessionStart`, Antigravity `on_session_start`) runs the audit *once,
  before the first turn*, with a cached MCP probe so it sees the real tool-
  schema cost. If the discretionary surface exceeds `[surface]
  max_static_tokens` in `ctx.toml`, it injects a bounded advisory naming the
  heaviest kinds, unused high-authority tools, broken dependencies, and the
  cheaper path. The probe is cached in `.ctx-surface/probe-cache.json` — first
  session ~6s, every session after ~0.08s.
- **Gateway as the MCP delivery.** `ctx wrap <host> --gateway` (or `ctx
  surface install-gateway --host <h> --apply`) points a host at **one** MCP
  server — `ctx surface gateway` — instead of the individual servers. It
  snapshots the backends to `.ctx-surface/backends.json` (the gateway reads
  them from there; the host loads only the gateway via a separate config file,
  never a rewrite of yours). At turn 1 the model sees only the compact index +
  `reveal`/`hide`; the thousands of tokens of tool schemas are **never sent**
  until a family is revealed. This is the only mechanism that makes it
  structurally impossible for unrevealed capability schemas to reach context.

`[surface]` in `ctx.toml`: `max_static_tokens` (8000), `gate` (off|warn),
`default_profile`, `gateway` (bool), `probe` (bool).

## Commands

```
# measure (Phase 1–2) — never mutates anything
ctx surface inventory        # every capability, priced, with usage + flags
ctx surface audit            # the summary scorecard
ctx surface explain <id>     # why one capability is visible, its cost + authority
ctx surface trim             # preview-only defer recommendations
ctx surface graph            # families, redundancy clusters, broken dependencies
ctx surface inventory --probe-mcp   # spawn MCP servers to measure real schema tokens

# compile (Phase 3) — the enforced cross-host boundary
ctx surface compile --profile local-dev --host claude [--apply]
ctx surface compile --profile read-only --host codex --apply
ctx surface compile --profile review    --host antigravity --apply

# disclose at runtime (Phase 4) — MCP gateway
ctx surface gateway          # stdio MCP server: index + reveal/hide + proxied backends

# reconcile (Phase 5) — shadow by default
ctx surface reconcile --intent "open a PR"   # recommend reveals/hides (logs shadow)
ctx surface reconcile --enforce              # apply through gateway state
ctx surface referee                          # score shadowed hides; promote/hold

# preventive wiring — make it 'bound before bloat'
ctx wrap setup --gateway                     # set up all 3 hosts + load ONLY the gateway
ctx surface install-gateway --host claude --apply   # gateway-only config for one host
# (the SessionStart gate is installed automatically by ctx wrap on every host)
```

## What it audits (and the honest blind spot)

The unit is any **persistent context-bearing capability**, not just a tool:

| Kind | Source it reads |
|---|---|
| `mcp_server` / `mcp_tool` | `.mcp.json`, `.claude/settings.json`, `.codex/config.toml`, `.agents/plugins/*/mcp_config.json` |
| `skill` | `.claude/skills/**`, `.agents/**/skills/**`, `.cursor/rules/**` |
| `agent` | `.claude/agents/*.md`, `.agents/**/agents/*.md` |
| `repo_instructions` | `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `GEMINI.md`, copilot-instructions |
| `policy` | `ctx.toml`, `ctx-policy.toml` |

**Blind spot, stated in the tool itself:** ctx cannot see the host's built-in
system prompt or native tool schemas (Read/Edit/Bash/Grep/Glob) — those live
inside the host binary/API, not in any file. The audit covers the
**discretionary** surface, which is exactly where over-provisioning,
redundancy, and capability leakage actually accumulate.

## What it measures

**1. Static token cost.** Every capability's contribution, deterministically
(`estimate_tokens`, ~4 bytes/token — no tokenizer dependency, byte-identical
across runs). For MCP servers the registration line is cheap; the real tax is
the tool schemas, measured only under `--probe-mcp`, which speaks MCP over
stdio (`initialize` → `tools/list`) to each server and prices each tool's
name + description + JSON schema. On our own bounded server that is the
difference between a 12-token registration line and the 490-token tool schema
— a 40× truer number.

**2. Utilization.** ctx sits in the hook path, so it already observes every
tool invocation. `ctx surface` folds the proxy wire log's per-tool counts back
onto the inventory: *"github.search_code — 418 tokens/turn, invoked 0 times
across N sessions."* That attribution is ctx's structural advantage; nothing
else sits where it can price a capability *and* see whether it was used.

**3. Authority.** A real property of **tools** (read → local-write →
remote-write → destructive), inferred from name and description. Prose (skills,
agents, instructions) has no authority of its own — it is `n/a`. A skill is not
"destructive" because it contains the word *delete*.

**4. Leakage**, classified rather than merely scanned:

- `excessive-authority` — an action tool that can mutate/destroy but is never used;
- `capability-mention:<tier>` — prose that surfaces a high-authority action into planning (behavioural/capability leakage — a skill *describing* deploy/delete biases tool choice even when denied at call time);
- `unrelated-domain:<domain>` — cloud/db/collaboration surface on a code task;
- `secret-adjacent` — env-var *names*, internal URLs, absolute paths, token shapes (the underscore rule keeps `GEMINI_API_KEY` in and ordinary acronyms like `README` out).

**5. Overlap** — descriptive clustering by shared capability key
(search/read/write/…). Marked shadow on purpose: semantic similarity is **not**
interchangeability (local-only ≠ remote-capable ≠ mutation-capable).

**6. Recommended disclosure level** (advisory) — L0 always-visible kernel
(ctx's own bounded tools, policy, repo steering) through L4 (destructive,
user-approval-gated). Computed from authority × utilization; never enforced in
Phase 1.

## Dogfooded on this repo

```
SESSION SURFACE AUDIT
────────────────────────────────────────────────────────
Discretionary context (host kernel excluded — blind spot):
  skill                5 ·   4,664 tok
  agent                1 ·     638 tok
  policy               1 ·     180 tok
  mcp_server           1 ·      12 tok      (490 tok under --probe-mcp)
  TOTAL                8 ·   5,494 tok/turn
Leakage flags: 4 capabilities
  skill.SKILL: capability-mention:remote-write
  agent.ctx-explorer: capability-mention:destructive
  skill.routing-policy: unrelated-domain:database
Preview-trim candidates: 1 · ~638 tok/turn recoverable (advisory — nothing hidden)
```

Receipt: [`evals/surface-audit-2026-07-20.md`](../evals/surface-audit-2026-07-20.md).

## The five phases (all shipped)

| Phase | What | Mutation? |
|---|---|---|
| 1 | inventory · accounting · utilization · overlap/leakage shadow · preview trim · MCP probe | none |
| 2 | dependency + overlap graph — families, `provides`/`requires`, redundancy clusters, broken dependencies | none |
| 3 | compile-time profiles (`ctx surface compile`) with dep-closure / provider / authority / budget checks → **minimal per-host config** | writes config on `--apply` |
| 4 | progressive-disclosure **MCP gateway** — compact family index + `reveal`/`hide` + `tools/list_changed`, proxying live backends | gateway state only |
| 5 | automatic reconciliation — hide unused high-cost family after a phase, reveal on intent; **never** remove a required or kernel family | shadow by default; `--enforce` opt-in |

Each phase's referee is its acceptance gate: Phase 1–2 are measurement (no
gate needed); Phase 3's checks must be clean before a config is trusted;
Phase 5 ships **shadow-first** with a paired referee (`ctx surface referee`)
that promotes a hide rule only once it never mis-hid a family used afterwards.

## Enforcement per host (the honest matrix)

Research finding: the only surface bound *every* host respects is set at
compile/launch time. Dynamic reveal is a best-effort affordance on top.

| Host | Compile (Phase 3) — enforced | Gateway reveal (Phase 4) — in-session? |
|---|---|---|
| **Claude Code** | `--strict-mcp-config --mcp-config .ctx-surface/mcp.claude.json` + `permissions.deny mcp__<dropped>__*` | **Yes** on the normal tool path (honours `list_changed`, v2.1+); the ToolSearch/deferred index does not refresh — reconnect to pick those up |
| **Codex** | `.ctx-surface/config.codex.toml` — only selected `[mcp_servers.*]` at startup | **No** — startup snapshot; a reveal applies after restart |
| **Antigravity** | `.ctx-surface/mcp_config.antigravity.json` — minimal servers | **Manual** — a reveal applies after the MCP *Refresh* |

**Config gates servers; only the gateway gates tokens per-tool.** Measured on
two live reference MCP servers ([`evals/surface-real-2026-07-20.md`](../evals/surface-real-2026-07-20.md)):
a `read-only` profile that keeps two servers (each has read tools it needs) but
defers 17 of their tools saves only **−99 tok** by whole-server config, versus
**−1,654 tok (~53%)** through the gateway — because a Claude `permissions.deny`d
tool is still *listed* to the model, so it still costs context. `ctx surface
compile` therefore reports **both** numbers and never claims the per-tool
figure for a host config that can't deliver it.

Wire the gateway on any host by registering **one** MCP server pointing at
`ctx surface gateway` instead of the individual servers — it fronts them all,
starts with just the index, and reveals per family. The ctx bounded retrieval
tool rides along automatically (the `harness` family is kernel, always
revealed). Everywhere the gateway is a single bounded entry point; only where
the client honours `list_changed` is the reveal *live* rather than
apply-on-reconnect. That is the same machinery that compiles a minimal
capability slice per delegated worker (coordinator / editor / explorer)
instead of duplicating a 30k-token surface into each.

## Why this composes

The set-cover framing — *task requires logical capabilities; each tool provides
a subset at a token/authority/confusion cost; choose the smallest adequate
surface* — is the eventual Phase 4 optimizer. Phase 1 deliberately builds the
**telemetry that optimizer will need** (real per-tool cost × observed use)
before building the optimizer, and the same audit becomes the substrate for
model routing: *cheap model succeeds at ≤8 tools / ≤4k schema tokens; strong
model required above that.* Capability auditing is not separate from routing —
it is the prerequisite.
