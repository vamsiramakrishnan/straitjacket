<sub><a href="README.md">« straitjacket / docs</a></sub>

# Configuration reference

Everything straitjacket reads from `ctx.toml`, in one place. New here? You
don't need any of this to start — `ctx setup` writes sensible defaults and
most workspaces never touch them. Reach for this page when you want to tune a
budget, tighten the guard, or add a monorepo scope.

## Where configuration lives

straitjacket reads **exactly one file: `ctx.toml` at the workspace root.** There
is no upward search and no user-global or machine-global config — configuration
is per-workspace and committed with the repo. `ctx init` (or `ctx setup`)
writes a starter `ctx.toml` and a `.ctxignore`.

The workspace root is resolved in this order: an explicit `--workspace` flag →
the path your host passes in → the nearest ancestor directory containing a
`ctx.toml` → `git rev-parse --show-toplevel` → the nearest ancestor with an
`.agents/` directory → the current directory.

**Missing or malformed config never breaks anything.** If `ctx.toml` is absent,
or contains a TOML syntax error, straitjacket silently falls back to the
built-in defaults documented below rather than erroring. Unknown keys inside a
known section are ignored. (One consequence worth knowing: a typo in `ctx.toml`
does not raise — it quietly reverts the affected setting to its default. If a
change seems to have no effect, check the file parses.)

## A minimal `ctx.toml`

```toml
version = 1

[budgets]
digest_tokens = 480
result_tokens = 1200
turn_retrieval_tokens = 2800
max_inline_bytes = 16384
digest_head_lines = 5
digest_tail_lines = 5

[guard]
mode = "guarded"               # advisory | guarded | strict
unknown_command = "force_ask"
internal_error = "allow"       # fail-open: a broken guard must not brick the workspace
```

Everything below is optional; the defaults shown are what you get without the
key.

## `[budgets]` — how much the model is allowed to see

Token and byte ceilings on every model-visible surface. Anything above a
ceiling is captured and shown as a bounded digest instead.

| Key | Default | Meaning |
|---|---|---|
| `digest_tokens` | `480` | Token budget for a digest. |
| `result_tokens` | `1200` | Token budget for a single retrieval (`ctx get`). |
| `turn_retrieval_tokens` | `2800` | Cumulative retrieval budget per turn. |
| `max_inline_bytes` | `16384` | Native-read size (input side) above which a read is bounded rather than pasted whole. |
| `max_inline_lines` | `240` | Line cap injected when an oversized read is rewritten to a bounded window. |
| `max_matches` | `80` | Maximum match count returned by a search. |
| `session_read_budget_bytes` | `262144` (256 KiB) | Cumulative native-read budget per session before graduated pressure kicks in. |
| `max_tool_output_bytes` | `16384` | Emission gate (output side): a tool result larger than this is digested before it reaches the model. |
| `window_pressure_pct` | `70` | Context-window fullness (%) at which budgets start tightening. |
| `failure_budget_factor` | `2.0` | Failing runs get this multiple of the standard digest budget — a failure carries the evidence you need. |
| `digest_head_lines` | `5` | Head lines shown for a generic text flood. |
| `digest_tail_lines` | `5` | Tail lines shown for a generic text flood. |

`max_inline_bytes` (input, native reads) and `max_tool_output_bytes` (output,
tool results) are deliberately separate knobs — tune the flood you actually
have.

## `[guard]` — how commands are steered

The guard is the PreToolUse classifier that decides what happens to each command
before it runs.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"guarded"` | `advisory` \| `guarded` \| `strict`. **`advisory` makes the guard a no-op** — nothing is steered. |
| `unknown_command` | `"force_ask"` | Disposition for a command the classifier doesn't recognize: `allow` \| `deny` \| `ask` \| `force_ask`. |
| `internal_error` | `"allow"` | What to do if the guard itself errors. The default is availability-safe (fail-open); set `"deny"` to fail-closed. |
| `steering` | `"auto"` | `auto` \| `rewrite` \| `deny`. `deny` disables all transparent rewrites, so you see plain denials instead of substitutions. |
| `collapse` | `true` | Master switch for the transparent command-substitution surface. Set `collapse = false` to break-glass it off. |
| `speculative_native` | `true` | On Claude Code and Codex only, let one explicitly named pytest node run without the `ctx run` wrapper while the session is passive and that signature has not flooded. The fail-closed PostToolUse gate still captures any result over `max_tool_output_bytes`. Set `false` to always capture tests at birth. |

Secret-bearing paths and outside-workspace access are **always** force-asked and
**never** rewritten, regardless of `steering` or `collapse`.

Before `unknown_command` is consulted, the command-span registry recognizes
bounded/structured queries for direct execution and known read-only noisy
commands for capture. Unknown and mutation-shaped commands intentionally remain
at the configured permission boundary. On hosts without input substitution,
capture is emitted as an exact bounded rerun instruction rather than being
executed transparently.

The speculative-native fast path never applies to whole suites, directories,
file-only pytest targets, shell expressions, active/high-pressure sessions, or
hosts without output substitution. An unexpected flood is digested once and
marks that signature so subsequent calls return to birth-time capture.
This policy is the first reviewed AlphaEvolve product canary; its measured
tradeoffs and remaining proof boundary are documented in the
[optimization guide](ALPHAEVOLVE-OPTIMIZATION.md#how-those-benefits-reached-the-product).

**To make the harness stricter,** `mode` and `steering` are different axes:
`mode` sets *how much* is classified (`advisory` off → `guarded` → `strict`),
while `steering` sets *what happens* to a flooding command — `auto`/`rewrite`
transparently reroute it through `ctx run`, and `steering = "deny"` blocks it
outright so nothing runs until you re-issue it yourself. A locked-down install
typically pairs `mode = "strict"` with `steering = "deny"`.

## `[workspace]` — capture boundaries

| Key | Default | Meaning |
|---|---|---|
| `allow_outside_root` | `false` | Permit access outside the workspace root. |
| `follow_symlinks` | `false` | Follow symlinks during capture. |
| `nested_repos` | `"separate"` | How nested git repositories are treated. |
| `respect_gitignore` | `true` | Honor `.gitignore` during capture. |

## `[engagement]` — graduated engagement

Controls how quickly the harness ramps from staying out of the way to actively
steering, so small sessions aren't taxed.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"auto"` | `auto` \| `active` \| `passive`. |
| `activate_after_calls` | `8` | Interception count at which a session graduates passive → active. |
| `lean_models` | built-in list | Models treated as "lean" (steered more conservatively). |

## `[store]` — the artifact store

| Key | Default | Meaning |
|---|---|---|
| `backend` | `"user-state"` | `user-state` \| `local`. |
| `retention_days` | `30` | How long artifacts are kept before garbage collection is eligible. |

The store lives outside the repo by default, under `~/.local/state/ctx` (or
`$CTX_STATE_HOME` / `$XDG_STATE_HOME` if set). `local` selects
`.ctx-session-reads/store` and is now an effective backend, not an advisory
hint. If user-state is read-only under a managed sandbox, ctx proves that with
an actual write, falls back to the local backend, and records a path-free sticky
route so later commands can still resolve the run handles already emitted.
`ctx doctor` reports the effective backend. It fails only when neither location
is writable; see [Troubleshooting](TROUBLESHOOTING.md).

## `[plan]` — bounds on compiled investigations

Ceilings a `ctx plan` / `ctx plan run` program may only *tighten*, never
exceed.

| Key | Default | Meaning |
|---|---|---|
| `max_nodes` | `24` | Maximum plan nodes. |
| `max_fanout` | `64` | Maximum plan fan-out. |
| `wall_seconds` | `120.0` | Wall-clock bound for a plan run. |
| `replans` | `1` | Allowed replans. |

## `[orchestrate]` — edit and prewalk policies

These optional keys control the [edit loop](EDIT-LOOP.md) and
[prewalk](PREWALK.md). See [routing](ROUTING.md) for the broader orchestration
configuration.

| Key | Default | Meaning |
|---|---|---|
| `prewalk` | `false` | Offer a frontier mutation worker a verified handoff when a cheaper installed model and another attempt are available. |
| `edit_policy_file` | `""` | Workspace-relative paired live JSONL evidence for edit-format advice. Insufficient evidence keeps native formatting. |
| `prewalk_policy_file` | `""` | Workspace-relative paired live JSONL evidence required before offering prewalk. Insufficient evidence keeps the assigned model. |

```toml
[orchestrate]
prewalk = true
edit_policy_file = "evals/edit-results.jsonl"
prewalk_policy_file = "evals/prewalk-results.jsonl"
```

Set `edit_shape` on each applicable route node to the evaluated task shape.
The exact model ID (or guide/executor pair) and shape must match the evidence.
An absent or unreadable configured file cannot select a new strategy. An empty
`prewalk_policy_file` leaves `prewalk = true` as an explicit experimental
opt-in, still subject to receipt verification and existing budget/attempt
limits. Policy files grant no additional mutation permissions. See
[paired evaluations](../evals/EDIT-MATRIX.md) for the evidence schema and gates.

## `[surface]` — the input side (MCP tool schemas)

Governs the discretionary-context budget and the pre-flight gate. That gate runs
on `SessionStart` for Claude Code and Codex; Antigravity has no such event, so
there it runs on `PreInvocation` and injects the advisory as an *ephemeral*
message (that hook fires before every model call, so a persistent one would
re-accumulate context on each).

| Key | Default | Meaning |
|---|---|---|
| `max_static_tokens` | `8000` | Discretionary-surface token budget per turn. |
| `gate` | `"warn"` | `off` \| `warn` — pre-flight gate mode. |
| `default_profile` | `""` | Profile suggested when over budget. |
| `gateway` | `false` | Use the MCP gateway delivery (progressive disclosure). |
| `probe` | `true` | Measure real MCP tool schemas (cached) during the gate. |

## `[redaction]` — secret redaction

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable secret redaction in model-visible output. |
| `patterns` | full built-in set | Active redaction patterns. |

A guard rail worth knowing: if `patterns` is set to something that isn't a list
(a bare string typo, say), straitjacket rejects it and falls back to the full
default set — a typo can't silently disable redaction.

## `[scopes]` — named monorepo roots

Define named scopes so `--scope <name>` selects a subtree:

```toml
[scopes.payments]
roots = ["services/payments", "libs/payments-common"]
```

Each `[scopes.<name>]` needs a `roots` list; a non-list `roots` is ignored.

## `[aliases]` — multi-workspace routing

Map `ws:<alias>` selectors to other workspace paths (absolute, or relative to
this root):

```toml
[aliases]
api = "../api-service"
```

Then address across workspaces with `ws:api/repo:src/main.py`.

## Secret denials and `.ctxignore`

A fixed built-in denylist always blocks capture of secret-bearing paths — you
cannot remove these, only add to them:

```
.env  .env.*  **/.env  **/.env.*  **/secrets/**  **/credentials/**
**/*.pem  **/*.key  **/id_rsa*  **/id_ed25519*  **/.aws/**
**/.config/gcloud/**  **/.ssh/**
```

`.ctxignore` at the workspace root **adds** to that list — one glob per line,
`#` comments and blank lines ignored. It is additive only: nothing you write can
remove a built-in secret denial. `ctx init` seeds it with the secret list plus
common noise (`node_modules`, `.venv`, `dist`, `build`).

## Verifying your configuration

```bash
ctx doctor                 # validate the install, store, hooks, and classifier
ctx doctor --antigravity   # also validate the Antigravity plugin
```

`ctx doctor` prints one `✓`/`✗` per check and exits non-zero if any fail. See
[Troubleshooting](TROUBLESHOOTING.md) for what each failing check means.

---

[Getting started](GETTING-STARTED.md) · [CLI guide](CLI.md) · [Troubleshooting](TROUBLESHOOTING.md) · [Concepts](CONCEPTS.md)
