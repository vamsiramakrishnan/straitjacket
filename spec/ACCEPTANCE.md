# CTX v0.1 acceptance suite

A release is not conformant until every MUST below is automated.

## Determinism

- MUST produce byte-identical digest output for identical artifact bytes, profile, policy, focus, and normalized invocation metadata.
- MUST remain identical across locale, timezone, absolute clone path, and process restart.
- MUST sort JSON keys and result coordinates canonically.

## Capture

- MUST preserve stdout and stderr separately.
- MUST retain non-zero exit code, signal, timeout, binary output, and invalid UTF-8.
- MUST leave no partially published manifest after crash; temporary writes are atomic.

## Budgets

- MUST cap every digest, search, get, stats, and MCP response.
- MUST reject or continue an oversized `get` request rather than returning it whole.
- MUST enforce per-turn cumulative retrieval budget when a conversation ID is available.

## Evidence plans

- MUST reject invalid plans statically with closed-vocabulary reasons (cycles via forward references, node/fan-out ceilings, guard grammar, capability class per tier) before any execution.
- MUST persist every executed node result as an addressable content-addressed blob and declare every skipped or failed node with a typed reason in the digest.
- MUST produce byte-identical investigation digests for an observe-only plan re-run against an unchanged worktree.
- MUST reject execute-class ops on the MCP tier and refuse `ast.rewrite.apply` when the generation changed since preview.
- MUST render the counterevidence section in every outcome, including its empty form.

## Repository behavior

- MUST pass Git repo, plain folder, monorepo scope, nested repo, submodule, symlink escape, deleted file, renamed file, and changed-after-search fixtures.
- MUST resolve the longest containing workspace in a multi-root hook payload.
- MUST refuse ambiguous roots.
- MUST never emit an absolute workspace/store path in a stable digest.

## Handles and retention

- MUST detect ambiguous short IDs.
- MUST scope handles to workspaces.
- MUST retain artifacts referenced by active leases/checkpoints during garbage collection.
- MUST not delete artifacts when the Antigravity plugin is uninstalled.

## Hook behavior

- MUST emit exactly one JSON object on stdout for every code path.
- MUST emit `{"decision":"allow"}` on internal failure in default guarded mode.
- MUST deny a known unbounded raw command with an executable `ctx run` remediation.
- MUST allow a command already routed through `ctx`.
- MUST use `force_ask` for configured outside-root and secret-path cases.
- MUST not depend on current working directory for locating the runtime.

## Skill and plugin

- MUST be discoverable from `.agents/skills/ctx-harness/SKILL.md` in standalone mode.
- MUST be discoverable from `.agents/plugins/ctx-harness/skills/ctx-harness/SKILL.md` in plugin mode.
- MUST fail doctor checks when both are installed.
- MUST validate `plugin.json`, `hooks.json`, and `mcp_config.json` as JSON.

## Security and redaction

- MUST strip ANSI/control sequences from model-visible output.
- MUST redact configured secret fixtures deterministically.
- MUST record that redaction occurred without printing the secret.
- MUST prevent `../` and symlink workspace escape unless explicitly authorized.

## Quality evaluation

Run at least four arms on CI debugging, incident logs, code search, and large structured API results:

1. raw inline;
2. post-hoc compression;
3. CTX digest plus retrieval;
4. source-side filtering plus CTX fallback.

Report zero-hop answer rate, evidence recall, final correctness, retrieval hops, model-visible tokens, cached/uncached input where available, and end-to-end latency.
