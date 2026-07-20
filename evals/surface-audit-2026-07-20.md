# Capability-surface audit — dogfood receipt

**Date:** 2026-07-20 · **Command:** `ctx surface {inventory,audit,trim} [--probe-mcp]`
**Subject:** straitjacket's own workspace surface.

This is the first receipt for the **input side** of containment (design:
[`docs/CAPABILITY-SURFACE.md`](../docs/CAPABILITY-SURFACE.md)). Phase 1 is
measurement only — nothing is hidden or removed.

## Inventory (static, no probe)

```
[ctx surface inventory · 8 capabilities]
   1,476 tok  skill      skill.verbs                  auth=n/a          used=—
   1,436 tok  skill      skill.SKILL                  auth=n/a          used=—  capability-mention:remote-write
   1,418 tok  skill      skill.evidence-plans         auth=n/a          used=—  secret-adjacent
     638 tok  agent      agent.ctx-explorer           auth=n/a          used=—  capability-mention:destructive
     194 tok  skill      skill.routing-policy         auth=n/a          used=—  unrelated-domain:database
     180 tok  policy     policy.ctx.toml              auth=read         used=—
     140 tok  skill      skill.repository-addressing  auth=n/a          used=—
      12 tok  mcp_server mcp.ctx-harness              auth=unknown      used=—
```

Totals: **8 capabilities · 5,494 static tokens/turn** of discretionary surface
(host kernel excluded — the stated blind spot).

## The MCP probe changes the MCP number 40×

Static inventory sees only the registration line for an MCP server (12 tok).
`--probe-mcp` speaks MCP (`initialize` → `tools/list`) to the server and prices
the actual tool schema:

```
      12 tok  mcp_server mcp.ctx-harness       (static: registration line only)
     490 tok  mcp_tool   mcp.ctx-harness.ctx   (probed: real name+desc+schema)
```

For a real multi-tool server (e.g. a GitHub MCP with ~15 tools) this is the
difference between an audit that reads "MCP: cheap" and the truth that MCP tool
schemas are frequently the single largest discretionary line.

## Audit + trim (preview only)

```
Leakage flags: 4 capabilities
  skill.SKILL:         capability-mention:remote-write
  skill.evidence-plans: secret-adjacent
  agent.ctx-explorer:  capability-mention:destructive
  skill.routing-policy: unrelated-domain:database
Preview-trim candidates: 1 · ~638 tok/turn recoverable (advisory — nothing hidden)
```

The one defer candidate is `agent.ctx-explorer` (recommended L2): it surfaces a
destructive action into planning and is not needed for read-only phases. The
recommendation is advisory — Phase 1 never hides it.

## Honesty notes

- **Heuristics are shadow signals.** `capability-mention` and `unrelated-domain`
  are keyword-derived; `secret-adjacent` uses shape patterns (the underscore
  rule keeps `GEMINI_API_KEY` in and `README`/`SPEC` out — verified in
  `tests/test_surface.py`). None of them mutate the surface.
- **Utilization is `—` here** because this workspace has no proxy wire log yet;
  in a live session the per-tool counts fold in and unused high-authority tools
  light up. The attribution path is tested against a synthetic wire log.
- **Determinism:** token counts are byte-derived (`estimate_tokens`), so the
  inventory is byte-identical across runs; the probe is the only non-static
  input and fails open to the static number.

Covered by `tests/test_surface.py` (12 tests, incl. a fake-MCP-server fixture
for the probe's JSON-RPC client).
