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

## Status: Phase 1 — measurement, not mutation

`ctx surface` **measures** the surface and **recommends**; it never hides or
removes anything. Enforcement (progressive disclosure) is deferred and will
ship only after the shadow signals here are validated. This is deliberate:
receipts precede doctrine, and silently trimming a capability the task needed
is the one failure mode worth being slow about.

```
ctx surface inventory        # every capability, priced, with usage + flags
ctx surface audit            # the summary scorecard
ctx surface explain <id>     # why one capability is visible, its cost + authority
ctx surface trim             # preview-only defer recommendations (nothing hidden)
ctx surface inventory --probe-mcp   # spawn MCP servers to measure real schema tokens
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

## The deferred phases (not yet built)

| Phase | What | Gate |
|---|---|---|
| 1 · **shipped** | inventory · accounting · utilization · overlap/leakage shadow · preview trim · MCP probe | this document |
| 2 | dependency + overlap graph (`provides`/`requires`/`authority`/`phase`) | — |
| 3 | compile-time profiles (`ctx surface compile --profile`) with authority/budget checks | — |
| 4 | progressive disclosure: compact family index + explicit `reveal`/`hide`, host dynamic registration where supported | shadow-proven trims |
| 5 | automatic reconciliation — hide unused high-cost family after a phase, reveal on intent trigger; **never** remove a capability an active task contract requires | paired referee |

Progressive disclosure needs a real enforcement mechanism, not just a
recommendation. The natural one is ctx acting as an **MCP gateway**: it already
speaks the protocol (Phase 1 probes with it), so it can front other servers,
present a compact family index, and reveal tools on demand via
`tools/list_changed`. That mirrors exactly what ctx does for evidence — a
boundary that reveals on demand — and it is the same machinery that would
compile a minimal capability slice per delegated worker (coordinator /
editor / explorer) instead of duplicating a 30k-token surface into each.

## Why this composes

The set-cover framing — *task requires logical capabilities; each tool provides
a subset at a token/authority/confusion cost; choose the smallest adequate
surface* — is the eventual Phase 4 optimizer. Phase 1 deliberately builds the
**telemetry that optimizer will need** (real per-tool cost × observed use)
before building the optimizer, and the same audit becomes the substrate for
model routing: *cheap model succeeds at ≤8 tools / ≤4k schema tokens; strong
model required above that.* Capability auditing is not separate from routing —
it is the prerequisite.
