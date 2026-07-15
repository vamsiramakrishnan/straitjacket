# straitjacket 🧥
An artifact-backed, repository-aware context containment harness and execution broker for the Antigravity engine.
straitjacket forces tight, unyielding structural boundaries on wild, unbounded tool outputs. When an AI agent attempts to run massive cat pipelines, recursive directory listings, or verbose test suites, straitjacket intercepts, digests, and summarizes the output, registering only a tiny, deterministic artifact handle in the model transcript.

## 🔒 The Core Invariant
> Every potentially unbounded operation MUST either execute inside straitjacket, returning a bounded artifact digest capabilitiy, or be flatly rejected before execution.

* **Zero Token Bloat**: Multi-megabyte outputs are captured at the source. The model transcript functions as an index over repository state and artifacts, not a warehouse of raw payload bytes.
* **Absolute Determinism**: PIDs, wall-clock durations, temporary paths, environment leakage, and locale differences are completely stripped from model-visible output. This drastically increases prompt reproducibility across sessions and providers.
* **Capability-Based Retainers**: The model navigates data using project-scoped, immutable HMAC tokens (ctx:api:run:K7BXRWQX2Y4N) instead of raw text.
* **Path Containment**: Maps multi-folder environments, nested Git structures, and worktrees to strict aliases (e.g., @api, @infra), rendering directory traversal escapes (../) physically impossible.

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

## 1. Deployment Strengths

| Mode | Integration | Guarantee |
|---|---|---|
| Skill Mode | SKILL.md only | **Advisory**: Agent is trained on protocol discipline but can bypass it. |
| Plugin Mode | Skill + MCP + PreToolUse Hooks | **Enforced**: Intercepts recognized tool paths; highly resistant to accidental bypass. |
| Native Harness | SDK Agent with raw built-ins stripped | **Structural**: Built-ins like run_command are removed; raw output cannot physically enter context. |
| Hardened Mode | Native Harness + Isolated Broker | **Isolation-Backed**: Broker runs under a separate OS identity/container; sandboxed shell cannot read the CAS database. |

## 🧭 Selector Grammar & Root Aliasing
Absolute host paths are entirely hidden from the model. Instead, straitjacket dynamically binds mounted paths to deterministic aliases at every invocation:

```
[ctx-scope:v1 id=cts_J7KBW4H2]
roots:
  @api       git-worktree  payments-api
  @infra     git-worktree  platform-infra
  @design    folder        product-design
default: none
```

The model targets files and processes using two crisp address spaces:

* **Repository Selectors**: Target live workspace tracks.
  * `@api` (Reference the root directory)
  * `@api:src/payments/service.py` (Target file within specific root)
  * `@infra:terraform/prod`
* **Immutable Artifact Handles**: Project-scoped HMAC capabilities derived from the underlying storage manifest hash.
  * `ctx:api:run:K7BXRWQX2Y4N` (Command output capture)
  * `ctx:api:search:4PTMDRYV6H2K` (Prior search index snapshot)
  * `ctx:api:evidence:A8JG9E3XM7PV` (Snapshotted file state slice)

## 🛠️ The Four Unified Verbs
The entire Model-facing MCP layer is frozen into one stable tool surface (**ctx**), handling operations entirely via parameter states instead of dynamic tool injection.

```json
{
  "name": "ctx",
  "description": "Execute and inspect repository or artifact data without placing unbounded output in model context.",
  "input": {
    "op": "run | search | get | stats",
    "scope": "Current ctx scope identifier",
    "root": "Mounted root alias without @",
    "target": "Repository selector or artifact handle",
    "argv": ["pytest", "-q"]
  }
}
```

### 1. run
Executes an arbitrary process argv directly (no ambient shell by default) inside the target root. Captures stdout and stderr concurrently, records exit codes, signals, and errors, and registers a bounded artifact digest.

```bash
ctx run --scope cts_J7KBW4H2 --root api --cwd services/payments -- focus "find test failures" -- pytest -q
```

Also acts as a passive file/directory producer proxy (e.g., `ctx run --file logs/incident.jsonl`) to swallow massive historical assets without piping through raw shell commands.

### 2. search
Executes multi-pattern queries targeting live files or historical artifact content. Supports regex, literal, jsonpath, and symbol searches. Matches are canonicalized and token-capped.

```bash
ctx search @api --scope cts_J7KBW4H2 --pattern 'TimeoutError' --glob '**/*.py' --context 3
```

### 3. get
Retrieves an exact, bounded slice of a repository file or historical artifact payload using lines, bytes, or structural coordinates.

```bash
ctx get ctx:api:run:K7BXRWQX2Y4N --span stdout:L8412-L8440
```

If a request exceeds the token window budget, it returns strict continuation tokens instead of silently dropping chunks.

### 4. stats
Exposes high-level metadata maps detailing repository layouts, tree sizes, languages, dirty git state parameters, or internal artifact shapes without leaking raw file context.

```bash
ctx stats @api --scope cts_J7KBW4H2
```

## 💾 Result Envelope & Digest Anatomy
Every operational verb yields a machine-readable envelope accompanying a canonical, heavily dense plaintext serialization intended for prompt optimization.

### Canonical Prompt Transcript View

```
[ctx:v1 api/run/K7BXRWQX2Y4N · pytest/v3]
source: @api/services/payments · HEAD 7f12cbe · dirty
command: pytest -q
status: exit 1
payload: stdout 48,211 lines · stderr 43 lines · ~62k tok
tests: 1,204
  passed: 1,187 · failed: 17 · skipped: 0

probable root failure:
  tests/risk/test_client.py::test_timeout
  stdout:L8412-L8427
  TimeoutError while calling risk-api

other failure signatures:
  TimeoutError                         11
  ConnectionResetError                 4

next:
  ctx search ctx:api:run:K7BXRWQX2Y4N --pattern 'TimeoutError'
```

## 🛡️ PreToolUse Gate Policy
When operating in Plugin or Native Harness configurations, straitjacket evaluates incoming tooling calls using a secure AST shell classifier (parsing POSIX shell, PowerShell, and cmd.exe).

* **Always Allowed**: ctx MCP infrastructure, localized low-overhead actions (pwd, git status --short), exact small file reads.
* **Denied & Redirected**: Raw cat, unbounded grep/rg, raw git diff, or direct execution of framework suites (npm test, cargo build) outside of a ctx run envelope.
* **Forced Confirmation**: Shell allocations, structural network streams, dynamic variable execution (eval), or repository crossings.

```json
{
  "decision": "deny",
  "reason": "Potentially unbounded output must be artifact-backed before execution. Reissue via: ctx run --scope cts_J7KBW4H2 --root api -- pytest -q"
}
```

## 📂 Source Code Layout

```
ctx-harness/
├── src/
│   └── ctx/
│       ├── cli.py               # User interface layer
│       ├── mcp.py               # MCP Server integration
│       ├── broker.py            # Local capability coordinator
│       ├── scope/               # Worktree, alias, and path isolation
│       ├── artifacts/           # CAS, catalogs (SQLite WAL), and leases
│       ├── execution/           # Subprocess runners and environment control
│       ├── digest/              # Profile classification engines (pytest, logs, etc.)
│       └── retrieval/           # High-speed search engines and line slicing
├── plugins/
│   └── antigravity/             # JSON configs, extension hooks, and rule assets
└── tests/                       # Complete determinism and security test fixtures
```

## 🚀 Setup & Installation

### Global Plugin Installation
To install the containment layer into your global Antigravity engine profile:

```bash
agy plugin install /path/to/straitjacket
```

### Repository Configuration
Commit a `.ctx.toml` file to your target project roots to specify token budgets, ignore patterns, and safety constraints:

```toml
schema = 1
mode = "guard"
digest_tokens = 450
evidence_tokens = 1200

[run]
timeout_seconds = 600
shell = "ask"
redact_names = ["*TOKEN*", "*KEY*", "*SECRET*"]
```

### Operational Checkup
Verify hook integrity, active path configurations, and broker access permissions at any time:

```bash
ctx doctor --antigravity
```
