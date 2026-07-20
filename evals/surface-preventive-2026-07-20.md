# Preventive surface containment — receipt

**Date:** 2026-07-20 · Subject: the two mechanisms that make the audit
preventive ('bound before bloat') rather than diagnostic, verified across
Claude Code / Codex / Antigravity.

## SessionStart gate — the harness notices before the first turn

On the live two-server surface (3,141 tok, budget set to 1,000):

```
$ ctx hook claude-code session-start   # (SessionStart payload on stdin)
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
 "CTX_SURFACE_GUARD: discretionary capability surface is 3,141 tokens/turn
  (budget 1,000). This is re-sent every turn before any tool runs.
    heaviest: mcp_tool 3,023tok, skill 81tok, policy 19tok
    broken deps: skill.reviewer→github
    → bound it: ctx surface compile --profile read-only --host <host> --apply
       or route MCP through `ctx surface gateway` for per-tool disclosure"}}
```

- First run probes the real MCP servers: **6.0s** (npx cold start).
- Cached run (`.ctx-surface/probe-cache.json`): **0.085s** — 70× faster.
- Under budget → `{"continue": true}` no-op. Gate `off` → no-op. Fail-open on a
  broken audit. Wired on all three hosts (Claude/Codex `SessionStart`,
  Antigravity plugin `SessionStart`).

## Gateway as delivery — schemas never enter until revealed

`ctx wrap <host> --gateway` on a 2-backend workspace (github + ctx-harness):

```
backends snapshot (.ctx-surface/backends.json): github, ctx-harness
claude   loads ONLY: ctx-surface-gateway
codex    loads ONLY: ctx-surface-gateway
antigravity loads ONLY: ctx-surface-gateway
```

At turn 1 the model sees `surface_index`, `surface_reveal`, `surface_hide` —
**not** the backend tool schemas. On the real filesystem+everything surface
that is 3 meta-tools instead of 27 tools / 3,023 tokens; the schemas load only
when a family is revealed. This is the structural version of the compile
finding (config can't cut tokens per-tool; the gateway can) turned into the
default delivery.

## Hardening (the gateway is now load-bearing)

- **Hang:** a never-answering backend returns `isError` in ~2s (select-based
  read deadline) instead of blocking the gateway forever.
- **Flood:** a 100,000-byte backend result is delivered as ~16 KiB + an honest
  note — the gateway composes capability containment with output containment.
- **Pagination:** `tools/list` follows `nextCursor` so paginated toolsets are
  fully seen.

## The symmetry, completed

```
INPUT  side:  large tool surface → SessionStart gate + gateway index → bound before bloat
OUTPUT side:  large tool output  → PreToolUse rewrite + digest       → capture before flood
```

Coverage: `test_surface_preflight.py` (13), `test_surface_gateway.py` (+5
install/hardening), `test_surface_compile.py`, plus the config policy test.
Full suite **924 passed**. All new code stdlib-only.
