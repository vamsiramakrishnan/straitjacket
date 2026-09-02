# CTX core and Antigravity integration — Draft target specification

**Version:** 0.1-draft  
**Date:** 2026-07-15  
**Keywords:** MUST, MUST NOT, SHOULD, SHOULD NOT, MAY follow RFC 2119 semantics.

**Status:** target design, not a complete description of the current release.
Known gaps include plaintext raw artifacts, configurable redaction, and the
absence of the `--execute-without-capture` escape path described below. For
shipped behavior, use the changelog, CLI and host-capability docs, code, and
tests.

**Scope:** core runtime behavior and Antigravity packaging. Host-hook statements
in this document are Antigravity-specific. Claude Code and Codex use their own
published hook contracts, the host adapters in `src/ctx`, and executable
acceptance tests. [ADR 005](adr/005-antigravity-hook-contract.md) records the
current Antigravity boundary.

## 1. Problem

Coding agents routinely place test logs, compiler output, repository listings, API responses, and large files directly into the conversation. That produces three failures:

1. old prompt bytes become expensive or impossible to reuse if history is later rewritten;
2. large evidence remains in the model-visible context on every later turn even when cached;
3. the model must retrieve relevant evidence from an increasingly noisy transcript.

CTX treats the transcript as an **index and control plane**. Full bytes live in an artifact data plane. Every model-visible result is bounded, deterministic, provenance-bearing, and retrievable by exact coordinates.

## 2. Product shape

CTX is not one prompt file. It has three layers:

| Layer | Artifact | Responsibility |
|---|---|---|
| Harness runtime | `ctx` executable and optional broker daemon | execute/capture, hash, parse, index, retrieve, enforce budgets |
| Skill | `skills/ctx-harness/SKILL.md` | teach the model when and how to use CTX |
| Plugin | `plugin.json`, `hooks.json`, `mcp_config.json`, embedded skill | install the behavior, route risky native tool calls before execution, expose bounded retrieval |

The **skill is policy**, the **plugin is routing and packaging**, and the **runtime is the trust boundary**.

## 3. Non-negotiable invariants

1. **Birth-time artifactization.** Potentially unbounded output MUST be captured before it is returned to the model.
2. **Prefix immutability.** The integration MUST NOT rewrite or re-digest prior model-visible turns.
3. **Bounded model output.** Every `ctx` operation MUST enforce an output byte and token budget.
4. **Deterministic views.** Given the same artifact bytes, focus query, profile version, and policy version, a digest MUST be byte-identical.
5. **Evidence provenance.** Every excerpt MUST carry an artifact reference and stable coordinates.
6. **Workspace confinement.** Repo operations MUST resolve against an explicit workspace and MUST NOT follow a path or symlink outside it unless the user explicitly authorizes that operation.
7. **No raw-store access.** Artifact paths MUST NOT be exposed to the model. Handles are capabilities, not filesystem instructions.
8. **Plugin-before-output routing.** A `PostToolUse` hook MUST NOT be treated as an output-transform mechanism. Commands and reads are routed through CTX in `PreToolUse` or are performed by a CTX-owned tool.
9. **No volatile prompt injection.** The default plugin MUST NOT inject timestamps, absolute paths, progress counters, or session noise into every model invocation.
10. **Durable history, bounded epochs.** The artifact/event ledger is append-only. A model conversation MAY be deliberately checkpointed into a new cache lineage instead of growing forever.

## 4. Antigravity packaging

### 4.1 Repo-scoped plugin

The generated workspace plugin lives at:

```text
<workspace>/.agents/plugins/ctx-harness/
├── plugin.json
├── hooks.json
├── mcp_config.json
└── skills/
    └── ctx-harness/
        ├── SKILL.md
        └── references/
            ├── routing-policy.md
            └── repository-addressing.md
```

The plugin MUST contain the skill so one installation activates all surfaces. It SHOULD NOT ship a global `rules/` file: an always-on rule consumes stable context, can collide with repository policy, and is unnecessary when the skill and hook are present.

The plugin MUST NOT claim permissions in `plugin.json`; Antigravity permissions are user-controlled. The installer SHOULD print recommended Ask/Allow settings but MUST NOT silently weaken them.

### 4.2 Standalone skill

The standalone subset lives at:

```text
<workspace>/.agents/skills/ctx-harness/
├── SKILL.md
└── references/
    ├── routing-policy.md
    └── repository-addressing.md
```

It contains no hook and no MCP registration. It is a degraded, advisory mode.

### 4.3 No duplicate installation

A workspace MUST install either:

- `.agents/plugins/ctx-harness/`, or
- `.agents/skills/ctx-harness/`.

Installing both creates duplicate routing metadata and can load two versions of the same behavioral contract.

## 5. Repo and folder semantics

CTX MUST work in a Git repository, a monorepo, a nested package, a submodule, or a plain folder.

### 5.1 Workspace resolution

For every operation, resolve the workspace in this order:

1. explicit `--workspace <path>`;
2. a workspace selected from the hook's `workspacePaths` by longest containing-path match against tool `Cwd`, `Path`, or `TargetFile`;
3. nearest ancestor containing `ctx.toml`;
4. `git rev-parse --show-toplevel`;
5. nearest ancestor containing `.agents/`;
6. current working directory as a plain-folder workspace.

If more than one Antigravity workspace is plausible and no target path disambiguates it, CTX MUST require an explicit workspace alias rather than guessing.

### 5.2 Stable workspace identity

Absolute paths MUST NOT appear in stable digests. Each workspace receives:

- a local opaque `workspace_id`, stored outside the repo;
- an optional committed `repo_key` in `ctx.toml` for cross-clone continuity;
- Git provenance when available: normalized remote identity, HEAD commit, worktree-state hash, and repo-relative CWD.

The content ID of an artifact MUST NOT depend on wall-clock time or machine-specific absolute paths.

### 5.3 Monorepos

`ctx.toml` MAY declare named scopes:

```toml
[scopes.payments]
roots = ["services/payments", "packages/risk-client"]

[scopes.web]
roots = ["apps/web", "packages/ui"]
```

A scope narrows search and tree operations; it does not create a separate artifact namespace unless configured. Results always use repo-relative paths.

### 5.4 Nested repositories and submodules

Default policy is `nested_repos = "separate"`. A path inside a nested Git root resolves to that root. A caller MAY force the parent workspace with `--workspace`.

### 5.5 Ignore and capture policy

Repository search SHOULD respect `.gitignore` by default and MUST additionally apply `.ctxignore`. Secret-bearing paths such as `.env*`, credential stores, private keys, and configured deny-globs MUST be excluded from automatic capture. Explicit inclusion requires a user-visible permission step.

## 6. Model-facing command surface

The core design retains four verbs. Their first argument is a generalized reference, not merely a blob ID.

```text
ctx run [--focus <query>] [--workspace <path>] -- <command> [args...]
ctx search <ref> <pattern>... [options]
ctx get <ref> <selector>
ctx stats <ref> [options]
```

Administrative commands such as `init`, `doctor`, `gc`, `pin`, `export`, `checkpoint`, and `antigravity install` are human/runtime operations and are not part of the stable four-verb model contract.

### 6.1 References

```text
run:7bd91f2a4c3d                 captured invocation manifest
run:7bd91f2a4c3d#stdout          exact stdout stream
run:7bd91f2a4c3d#stderr          exact stderr stream
blob:fe21c91ad4e8                raw immutable content
repo:                            current workspace
repo:src/payments/service.py     current file, snapshot-on-read
repo:services/payments           current subtree
ws:api/repo:src/main.py          explicit workspace alias in multi-root mode
```

Displayed IDs use at least 12 hexadecimal characters. Resolution MUST detect ambiguity and require expansion. Full integrity uses SHA-256 or stronger.

### 6.2 `ctx run`

`ctx run` MUST:

- execute without a shell by default, preserving canonical argv;
- require an explicit `--shell` mode for shell syntax;
- resolve CWD relative to the workspace;
- stream stdout and stderr into distinct immutable blobs;
- retain exit code, signal, timeout state, and canonical invocation metadata;
- generate a bounded digest after process completion;
- preserve Antigravity's permission flow by being invoked through the native command tool unless an SDK host provides equivalent permission mediation;
- support deterministic `--focus` evidence selection;
- handle non-UTF-8 output and binary streams without emitting arbitrary bytes to the model.

Interactive TTY programs are out of scope for capture mode. CTX MUST refuse them or run an explicitly selected passthrough mode that does not claim transcript protection.

### 6.3 `ctx search`

`ctx search` MUST support multiple patterns in one request and SHOULD support:

- regex and fixed strings;
- any/all semantics;
- bounded before/after context;
- path globs and repo scopes;
- stream selection;
- JSON Pointer/JSONPath, jq-like predicates, and structured-log fields;
- tree-sitter symbols when a parser is available.

Results MUST be deterministically ordered by workspace, path/stream, coordinate, and pattern index. Output MUST report scanned coverage, match count, shown count, and truncation.

Searching `repo:` performs snapshot-on-read: every returned file version is placed in the artifact store so later `get` operations remain stable even if the working tree changes.

### 6.4 `ctx get`

Selectors include:

```text
--lines A:B[@anchor]
--bytes A:B
--records A:B
--json-pointer /items/57
--symbol package.module:function
--span <opaque-coordinate>
--hashlines                     (render modifier; combines with --lines)
```

A request larger than the configured result budget MUST return a bounded preview plus continuation coordinates; it MUST NOT silently flood the transcript.

**Content anchors.** `--lines` MAY carry a content anchor (`A:B@<anchor>`), a
short digest over the addressed lines' content and nothing else — never their
position. Anchors apply to `--lines` only; a selector addressing an immutable
byte or record offset MUST reject one rather than ignore it.

Resolving an anchored span MUST take exactly one of three outcomes:

- the anchored content is at `A:B` — the result MUST be byte-identical to the
  unanchored resolution of the same span;
- the anchored content is elsewhere in the target — the result MUST return that
  content, declare the move, and echo the corrected address;
- the anchored content is absent — resolution MUST fail non-zero and MUST NOT
  return the current contents of `A:B`.

An implementation MUST NOT emit an address that is less verifiable than the one
that produced it: a continuation offered from an anchored resolution MUST itself
carry an anchor. Addresses to immutable content (`run:`, `blob:`, `snapshot:`)
SHOULD NOT carry anchors, whose guarantee that ref kind already provides.

### 6.5 `ctx stats`

Profiles MAY emit exact or approximate schema statistics. Every field MUST be labeled `exact` or `approximate`. High-cardinality operations use deterministic bounded sketches and samples.

### 6.6 `ctx plan` / `ctx plan run`

Compiled evidence plans (`ctx.plan/v1`, docs/EVIDENCE-PLANS.md): a model-authored, total, bounded DAG of logical evidence operations, validated statically and executed locally; one investigation digest (`investigate/v1`) returns.

- Validation MUST be static and total: cycle-free by construction (edges reference earlier steps only), node count and fan-out capped, `when` guards restricted to the count/outcome micro-grammar, rejection reasons drawn from a closed vocabulary.
- `ctx plan price` MUST render the cost card before execution (priced-context rule); nothing executes during `validate` or `price`.
- Every node result MUST persist as a content-addressed `ctx.plan-node/v1` blob; every non-executed node MUST be declared in the digest coverage section with its typed reason. The digest always renders.
- Physical engine selection is the harness's choice, deterministic given availability, and disclosed per node; the plan IR carries no engine field.
- Execute-class ops (`test.run`, `ast.rewrite.*`) run only on the CLI tier under the standard guard; the MCP `investigate` op MUST reject them at validation (bounded-only surface, §10.4). `ast.rewrite.apply` MUST be transactional and MUST refuse when the source-state generation changed since preview.
- The digest renders against `contracts/investigate.toml` through the shared resolver; the counterevidence section is REQUIRED in every outcome, including the empty form.

## 7. Invocation and artifact data model

A command produces an invocation manifest referencing independently addressed streams:

```json
{
  "schema": "ctx.invocation/v1",
  "id": "sha256:<manifest-hash>",
  "workspaceId": "ws_<opaque>",
  "cwd": "services/payments",
  "argv": ["pytest", "-q"],
  "shell": false,
  "result": {"exitCode": 1, "signal": null, "timedOut": false},
  "streams": {
    "stdout": {
      "blob": "sha256:<hash>",
      "bytes": 8421012,
      "lines": 48211,
      "mediaType": "text/plain",
      "encoding": "utf-8"
    },
    "stderr": {
      "blob": "sha256:<hash>",
      "bytes": 7270,
      "lines": 43,
      "mediaType": "text/plain",
      "encoding": "utf-8"
    }
  },
  "source": {
    "gitHead": "<commit-or-null>",
    "worktreeHash": "sha256:<state-hash-or-null>"
  },
  "digest": {
    "profile": "pytest/v1",
    "policy": "default/v1",
    "focusHash": "sha256:<normalized-focus-or-empty>",
    "bytesHash": "sha256:<exact-emitted-digest>"
  }
}
```

Operational access timestamps and retention metadata are stored separately and do not participate in content identity.

## 8. Deterministic digest contract

A digest is a function of:

```text
artifact bytes
+ normalized invocation metadata
+ digest profile version
+ policy version
+ optional normalized focus query
```

It MUST NOT include:

- current time or generated timestamps;
- elapsed-time progress noise in the stable view;
- absolute paths;
- random samples;
- locale-dependent sorting or formatting;
- unstable object-key order;
- ANSI control sequences.

The raw artifact remains byte-exact. A parser MAY normalize line endings or encoding only inside a derived view whose profile version records that behavior.

### 8.1 Digest shape

```text
[ctx run:7bd91f2a4c3d profile=pytest/v1]
cwd: services/payments
command: pytest -q
exit: 1
stdout: 48,211 lines · 8.0 MiB · est 62k tokens
stderr: 43 lines · 7.1 KiB
summary:
  tests: 1,204 · passed 1,186 · failed 3 · skipped 15
  first failure stdout:L8412-L8422: test_payment_timeout
  terminal failure stderr:L31-L43: AssertionError risk-api deadline
coverage:
  parsed: 48,211/48,211 lines
  shown: 2 spans · omitted: 48,188 lines
next:
  ctx search run:7bd91f2a4c3d 'risk-api' 'timeout' --context 3
  ctx get run:7bd91f2a4c3d#stdout --lines 8412:8422
```

The digest SHOULD present multiple competing evidence views rather than anchoring only on the first error: first failure, terminal failure, modal signature, anomalous burst, and command-specific root-cause candidates.

## 9. Profile registry

The runtime SHOULD ship deterministic profiles for:

- generic text and binary;
- JSON, JSON Lines, YAML, CSV, and tabular output;
- pytest, unittest, Jest/Vitest, Go test, Maven/Gradle, Cargo;
- compilers and linters;
- Git status/diff/log;
- build systems and package managers;
- structured application logs;
- directory trees and language inventories.

Profile detection MUST be deterministic and explainable. A profile MAY decline and fall back to `text/v1`.

## 10. Plugin behavior

### 10.1 Skill routing

Antigravity loads skill metadata first and the body only when relevant. The skill description therefore explicitly mentions commands, tests, builds, logs, API responses, repository searches, large files, and context/prompt-cache protection.

### 10.2 `PreToolUse` guard

The plugin installs a named `PreToolUse` hook. It matches native command and filesystem tools and inspects the tool call before any payload is emitted.

The hook classifies a call as:

- `allow`: already invokes `ctx`, has a statically proven bound, or reads a small file under the configured threshold;
- `deny`: a known high-flood operation with a direct CTX remediation;
- `force_ask`: ambiguous operation, outside-workspace access, secret path, or explicit raw-read request.

Example denial reason:

```text
CTX_CONTEXT_GUARD: this command may emit unbounded output.
Run it as: ctx run -- <original argv>
Then use ctx search/get/stats on the returned handle.
```

The hook MUST emit exactly one valid JSON object on stdout. Because an invalid or failed `PreToolUse` hook can block all tool use, default context-protection mode is fail-open on internal hook errors:

```json
{"decision":"allow"}
```

A security-oriented installation MAY choose fail-closed, but that is a separate policy and must be explicit.

### 10.3 No transparent post-hoc repair

`PostToolUse` MAY record telemetry, but it MUST NOT be represented as preventing transcript pollution. The raw result has already been created. The default plugin omits `PostToolUse` to avoid per-tool latency and false confidence.

### 10.4 MCP server

The plugin registers:

```text
ctx mcp --bounded-only
```

The MCP server exposes one stable tool schema with an `op` discriminator. It permits bounded `search`, `get`, `stats`, repository inspection, and administrative health checks. Arbitrary command execution remains `ctx run` through Antigravity's native command tool in v1 so normal command permissions remain visible to the user.

A future Antigravity SDK harness MAY expose `run` directly if it preserves equivalent permission and sandbox semantics.

### 10.5 No per-invocation prompt injection

The default plugin does not use `PreInvocation` to inject repository status, clocks, or reminders. This avoids volatile prompt bytes and repeated context. Repo state is returned only when a CTX operation is requested.

## 11. Hook classifier policy

The classifier is conservative and config-driven; it is not a shell-security parser.

### 11.1 Always route through CTX

Examples include:

- test, build, lint, type-check, benchmark, and package-manager commands without a proven output cap;
- `find`, recursive listings, broad `rg`/`grep`, `git log`, large diffs, logs, traces, and API/CLI dumps;
- `cat` or native file reads above `max_inline_bytes`;
- broad cloud/container commands such as logs, describe-all, get-all, and plan outputs;
- any command whose output risk is unknown in strict mode.

### 11.2 Native bounded operations

Examples include:

- `git status --short` under a configured entry cap;
- `head -n N`, `tail -n N`, and exact line slices where `N` is below policy;
- reads of small text files;
- commands that redirect output to a file and return a bounded status, provided the follow-up read is also routed through CTX.

A shell pipeline containing `head` is not automatically safe: upstream tools can still have side effects, and shell parsing is complex. The runtime records argv/shell mode and the hook errs toward `force_ask` for ambiguous shell expressions.

## 12. Store architecture

### 12.1 Default location

The default store is outside the repository:

```text
<XDG_STATE_HOME>/ctx/
└── workspaces/<workspace-id>/
    ├── blobs/sha256/ab/cd...
    ├── manifests/
    ├── indexes/
    ├── leases/
    ├── locks/
    └── audit/
```

Platform adapters map this to appropriate macOS and Windows user-state locations.

The repo contains only:

```text
ctx.toml       committed policy
.ctxignore     committed capture exclusions
```

A local `.ctx/` store MAY be supported in advisory mode, but the runtime MUST label it as bypassable because an unrestricted shell can read it directly.

### 12.2 Capability boundary

Handles are scoped to workspace and tenant/user. A raw content hash is not authorization. The broker MUST reject cross-workspace access unless a handle was explicitly exported or shared.

### 12.3 Retention

Manifests referenced by an active transcript or checkpoint receive leases. Garbage collection MUST be mark-and-sweep over leases, pins, and exported checkpoints. Deleting the plugin MUST NOT delete artifacts.

### 12.4 Secrets

Raw artifacts may contain secrets. The runtime SHOULD encrypt raw blobs at rest with an OS-keystore-backed key. Model-visible digests and excerpts MUST pass a deterministic redaction layer. The digest declares when redaction occurred without revealing the secret.

## 13. Repository configuration

A committed `ctx.toml` controls policy. See `examples/ctx.toml`.

Important defaults:

```toml
version = 1

[workspace]
allow_outside_root = false
follow_symlinks = false
nested_repos = "separate"

[budgets]
digest_tokens = 480
result_tokens = 1200
turn_retrieval_tokens = 2800
max_inline_bytes = 16384
max_inline_lines = 240

[guard]
mode = "guarded"
unknown_command = "force_ask"
internal_error = "allow"

[store]
backend = "user-state"
retention_days = 30
```

A project MAY make budgets stricter but SHOULD NOT silently raise them in a branch without review.

## 14. Checkpoints and cache epochs

Prompt caching does not remove old evidence from logical attention. CTX therefore tracks evidence packets and SHOULD recommend an epoch checkpoint when the configured transcript evidence budget is exceeded.

A checkpoint contains:

- task goal and current state;
- decisions and rationale;
- unresolved hypotheses;
- artifact handles and exact evidence coordinates;
- searches already attempted, including negative searches;
- files changed and verification status.

The old conversation and artifact ledger remain immutable. The user or SDK harness starts a new Antigravity conversation from the deterministic checkpoint. The external Antigravity plugin does not assume a compaction hook exists.

## 15. Failure behavior

| Failure | Required behavior |
|---|---|
| `ctx` missing | Skill reports degraded mode; hook allows rather than bricking the workspace, unless strict policy says otherwise |
| Store unavailable | `ctx run` fails before executing by default; `--execute-without-capture` requires explicit user approval |
| Parser failure | preserve artifact; use deterministic generic profile; label parser error |
| Ambiguous short ID | refuse and show candidate full IDs |
| Working file changed after search | retrieve snapshot captured at search time and label current-worktree divergence |
| Output budget exceeded | return truncation metadata and continuation coordinates |
| Secret detected | redact model-visible content; preserve encrypted raw bytes subject to policy |
| Hook timeout/error | emit one valid decision according to configured internal-error policy |
| Multi-root ambiguity | require `ws:<alias>` or `--workspace` |

## 16. Security model

CTX is context and evidence infrastructure, not a shell-security sandbox.

- Native `ctx run` MUST remain subject to Antigravity/user command permissions.
- Plugin manifests MUST NOT fabricate permission declarations.
- Workspace confinement prevents accidental path escape but does not protect against a deliberately authorized arbitrary command.
- A store outside the workspace improves isolation only while the agent sandbox prevents arbitrary user-home reads.
- Running Antigravity with unrestricted permissions weakens the no-raw-store invariant.
- Tool output is untrusted data; digests and excerpts must mark it as evidence and sanitize terminal/control sequences.

## 17. Observability

Telemetry MUST be separate from stable digests. Record:

- raw and digest bytes;
- estimated prompt tokens avoided;
- zero-hop answer rate;
- retrieval count and p95 hops;
- shown/scanned evidence ratio;
- hook allow/deny/force-ask counts;
- bypass attempts;
- parser/profile hit rate;
- secret redactions;
- final answer correctness in evaluation runs.

Do not place timing, counters, or telemetry IDs into deterministic model-visible output unless explicitly requested.

## 18. Acceptance gates

A release is conformant only if it passes `acceptance/ACCEPTANCE.md`, including:

- byte-identical digest replay;
- repo, plain-folder, monorepo, nested-repo, symlink, and multi-root cases;
- stdout/stderr separation;
- file mutation after snapshot;
- hook fail-open and strict fail-closed modes;
- bounded output under adversarial selectors;
- secret redaction;
- plugin and standalone-skill discovery;
- no duplicate skill installation;
- no raw absolute store paths in model output.

## 19. Delivery sequence

### Phase 1 — deterministic CLI

Implement workspace resolution, content-addressed capture, text/JSON/log/test profiles, four verbs, budgets, and schemas.

### Phase 2 — repo-scoped Antigravity plugin

Implement rendered plugin install, `PreToolUse` classifier, bounded MCP server, and doctor command.

### Phase 3 — broker and policy hardening

Move blobs outside the workspace, add leases, encryption, secret redaction, capability handles, and multi-root support.

### Phase 4 — SDK-native tool-result ABI

Provide a wrapper for Antigravity SDK tools so unbounded custom tool responses are artifactized inside the execution pipeline rather than relying on command routing.

### Phase 5 — adaptive economics

Use telemetry to choose inline vs artifact mode based on output size, expected remaining turns, retrieval probability, slice size, prefix size, and latency sensitivity. A static byte threshold remains the safe fallback.

## 20. Final architecture statement

> In Antigravity, CTX is a repo-aware harness whose plugin routes risky operations before execution, whose skill supplies the behavioral contract, and whose runtime turns every unbounded result into an immutable artifact plus a bounded deterministic view. The repository stores policy; the broker stores payloads; the transcript stores only handles and evidence.
