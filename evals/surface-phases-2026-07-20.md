# Capability surface — all five phases, working receipt

**Date:** 2026-07-20 · Design: [`docs/CAPABILITY-SURFACE.md`](../docs/CAPABILITY-SURFACE.md).

The input side of containment, end to end. Phases 1–2 measure, 3 enforces at
compile time, 4 discloses at runtime, 5 reconciles in shadow. Verified across
Claude Code / Codex / Antigravity config formats.

## Phase 2 — dependency + overlap graph (this repo)

```
CAPABILITY GRAPH
Families:
  harness      4 · 3,104 tok
  other        3 · 2,250 tok
  repository   1 ·   140 tok
Broken dependencies: none
```

On a fixture where a skill names `mcp__jira__create_issue` and no jira server
is configured, `graph` flags the broken dependency; a `mcp__github__…`
reference with github configured does not (regression-tested).

## Phase 3 — compile, enforced per host

`ctx surface compile --profile read-only` on a github + jira + ctx fixture,
run for each host, drops the remote/collab servers and keeps the ctx kernel:

```
host=claude       drop servers: github, jira   → .ctx-surface/mcp.claude.json + permissions.deny + --strict-mcp-config
host=codex        drop servers: github, jira   → .ctx-surface/config.codex.toml ([mcp_servers.ctx-harness] only)
host=antigravity  drop servers: github, jira   → .ctx-surface/mcp_config.antigravity.json
checks: clean (dep closure · providers · authority · budget)
```

Emitted JSON/TOML validated by parser in tests. `local-dev` on this repo:
static **5,494 → 3,244 tok/turn**.

## Phase 4 — gateway, live progressive disclosure

Full stdio round trip against a fake github backend (`tests/test_surface_gateway.py`):

```
tools/list  (before reveal) → surface_index, surface_reveal, surface_hide
call surface_reveal {family: remote-source-control}   → emits notifications/tools/list_changed
tools/list  (after reveal)  → … + mcp__github__search_code
call mcp__github__search_code {q: needle} → proxied to live backend → "hits:needle"
call surface_hide  → tool removed again
```

Reveal persists to `.ctx-surface/gateway-state.json`; kernel (`harness`,
carrying the ctx tool) is always revealed and unhideable.

## Phase 5 — reconcile, shadow + referee

```
reconcile (phase=explore, remote-source-control revealed & unused)
  hide  remote-source-control (~2,000 tok) — unused in phase explore (belongs to deliver)
  shadow only — pass --enforce to apply (referee-gate first)

--enforce  → gateway state: [harness]   (remote-source-control hidden)

referee  → hides scored 1 · safe 1 · unsafe 0 · verdict promote · promotable: remote-source-control
```

Governing law is enforced in code and tests: a family an active contract
requires, and the kernel, are never hidden — even when unused. When later
usage contradicts a shadowed hide, the referee returns `hold`, not `promote`.

## Coverage

`tests/test_surface.py` (17) · `test_surface_compile.py` (11) ·
`test_surface_gateway.py` (10) · `test_surface_reconcile.py` (12). Full suite
**906 passed**. All new code is stdlib-only (the `minimal` CI job covers it).
