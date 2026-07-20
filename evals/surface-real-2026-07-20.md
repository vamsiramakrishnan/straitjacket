# Capability surface on a REAL surface — receipt

**Date:** 2026-07-20 · Subject: two live reference MCP servers
(`@modelcontextprotocol/server-filesystem` + `server-everything`, run via
`npx`) plus realistic skills/agents. First outcome-validated run of the input
side — not a fixture.

## The surface (measured, not modeled)

`ctx surface audit --probe-mcp` spawned both servers, spoke MCP, and priced
every advertised tool from its real name + description + JSON schema:

```
Discretionary context (host kernel excluded — blind spot):
  mcp_tool   27 · 3,023 tok      ← real schemas from two live servers
  skill       2 ·    81 tok
  agent       1 ·    18 tok
  policy      1 ·     3 tok
  TOTAL      31 · 3,125 tok/turn
```

Real per-tool costs, e.g. `filesystem.read_text_file` 203 tok,
`everything.gzip-file-as-resource` 236 tok, `everything.get-annotated-message`
118 tok. Authority inferred per tool (read / local-write / remote-write /
unknown). No fabricated numbers.

## What the graph found on real data

```
Families: repository 16 · other 13 · deployment 1 · harness 1
Redundancy clusters (shadow):
  read   read_file, read_text_file, read_media_file, read_multiple_files
  list   list_directory, list_directory_with_sizes, list_allowed_directories
  get    8 everything.get-* tools
Broken dependencies:
  skill.reviewer → github     ← references mcp__github__create_pull_request,
                                 no github server configured
```

The broken-dependency finding is real and actionable: a shipped skill tells the
model to use a GitHub tool the workspace does not provide.

## The finding that mattered: config can't do per-tool token cuts

`ctx surface compile --profile read-only` on this surface, honestly:

```
enforced by config (drop whole servers): 3,125 → 3,026 tok (−99)
with `ctx surface gateway` (per-tool):   3,125 → 1,471 tok (−1,654)
keep servers: everything, filesystem
defer 17 tools within kept servers
```

Both servers stay (each has read tools the profile needs), so **dropping whole
servers saves almost nothing (−99)**. The real 53% reduction (−1,654) requires
gating *individual tools within a kept server* — and on Claude Code a
`permissions.deny`d tool is still listed to the model, so it still costs
context. **Only the gateway, which controls exactly what it lists, achieves the
per-tool token reduction.** The real surface proved why Phase 4 exists.

This surfaced a genuine honesty bug in the first cut (which reported the −1,654
number as if config achieved it). Compile now reports **both** numbers and
labels which host mechanism delivers each:

- **Claude:** whole-server drop only for tokens; per-tool `permissions.deny` is
  callability, not context savings → use the gateway.
- **Codex:** per-tool `disabled_tools` emitted (17 tools) — may reduce exposure
  depending on version.
- **Antigravity:** whole-server only; per-tool defers noted as gateway-only.

## Gateway, against a real server

`ctx surface gateway` fronted the live filesystem server: started with only the
index + reveal/hide (0 backend tools visible, ~0 backend tokens), and on
`surface_reveal repository` surfaced and proxied the real `read_file` call end
to end. That is the −1,654 recovered in practice, on a real server.

## Honesty ledger

- Token counts are byte-derived and deterministic; the only live input is the
  probe, which fails open to the static number.
- `server-everything` is a *reference* server; a production GitHub MCP (~40–70
  tools) would show a proportionally larger tax and gateway win.
- Covered by `tests/test_surface_compile.py::test_two_number_honesty_*` (real
  fake-server fixture) and the gateway suite. Full suite green.
