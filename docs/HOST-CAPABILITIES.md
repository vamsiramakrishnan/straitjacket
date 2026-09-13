# Host capabilities — what each agent can actually enforce

Straitjacket is the sidecar; the hosts below are the coding harnesses. Each
integration connects the sidecar's evidence tools to the user's existing agent.
The optional `antigravity-sdk` entry is a separate ctx-owned agent runtime,
not a requirement for using the sidecar with Google's Antigravity CLI.

**Status:** current implementation and published host contracts. Claude Code
and Antigravity have live receipts; the Codex gates are contract-tested but
still await a live CLI run. **Antigravity contract source:**
[`spec/adr/005-antigravity-hook-contract.md`](../spec/adr/005-antigravity-hook-contract.md)

> **New here?** `ctx` is straitjacket's command — the project is straitjacket,
> the binary is `ctx`. If `ctx run`, `ctx wrap` or "digest" are unfamiliar, read
> [How it works](HOW-IT-WORKS.md) first: ten minutes, one command walked through
> the whole system.

Keep using your preferred coding agent. Straitjacket's integrations range from
explicit MCP tools to automatic command interception, depending on the adapter
implemented and the host's API. CLI capture and the verified edit workflow are
available to any agent with terminal access.

## The two gates

Containment happens at two points:

- **The birth gate** (`PreToolUse`) fires *before* a command runs. On a
  rewrite-capable host, the command is routed through `ctx run` and captured in
  full. On Antigravity's deny-only path, the command does not run and nothing is
  captured until the agent explicitly reissues the bounded command.
- **The output gate** (`PostToolUse`) fires *after* a tool returns. It is the
  safety net: if something slipped past the birth gate, an oversized result is
  replaced by a bounded digest before the model sees it.

A host needs a specific API for each. The birth gate needs a way to *rewrite a
tool's arguments*; the output gate needs a way to *replace a tool's result*.

## What each host enforces

| host | birth gate | output gate | how |
|---|---|---|---|
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/claude.svg" width="24" height="24" alt=""> **claude** (Claude Code) | ✅ rewrites transparently | ✅ replaces the result | `updatedInput` / `updatedToolOutput` |
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/codex.svg" width="24" height="24" alt=""> **codex** (Codex CLI) | 🧪 implemented + contract-tested | 🧪 implemented + contract-tested | live CLI receipt pending |
| <img src="../assets/agents/antigravity.png" width="24" height="24" alt=""> **antigravity** (`agy` CLI) | ⚠️ **denies** and names the command | ❌ **none** | see below |
| **antigravity-sdk** (ctx's own agent) | ✅ bounded inside the tool | ✅ bounded inside the tool | see below |
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/hermes.svg" width="24" height="24" alt=""> **hermes** (Nous Hermes Agent) | Contract-tested argument rewrite | Contract-tested text replacement | `pre_tool_call` / `transform_tool_result` |
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/omp.svg" width="24" height="24" alt=""> **omp** (Oh My Pi) | Contract-tested argument rewrite | Contract-tested text replacement | `tool_call` / `tool_result` |
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/opencode.svg" width="24" height="24" alt=""> **opencode** | Contract-tested argument rewrite | Contract-tested text replacement | `tool.execute.before` / `tool.execute.after` |
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/dsh.svg" width="24" height="24" alt=""> **dsh** (DeepSeek Harness) | Deny and name the bounded retry | Contract-tested text replacement | `tools/pre-execute` / `tools/post-execute` |

The four new integrations have configuration, executable plugin, and subprocess
tests, not live model-session receipts. All seven agents can use the optional
[ACP worker transport](ACP.md) after endpoint/model setup. Every ACP session
receives `ctx` retrieval and `ctx_edit` patch/rewrite tools. Native interception
requires the adapter to load the host's plugin; receiving ACP tool updates alone
does not provide interception. See [setup and removal](AGENT-INTEGRATIONS.md).

The new plugins bound **text** results, preserving non-text blocks and metadata.
OMP's direct eval/browser bridges do not emit these tool hooks, and its error
path can rethrow the original error after the callback. DSH cannot rewrite
sealed input arguments; its post-hook also preserves a downstream plugin's
canonical-value rewrite rather than substituting stale presentation text.
These paths still have explicit `ctx run` and verified edit tools available.

On Claude Code, containment is invisible: you type `pytest -q`, the hook
silently substitutes `ctx run -- pytest -q`, and the agent never sees a refusal.
The Codex implementation targets the same experience; call it live only after a
version-pinned Codex run records both gates.

## Why Antigravity is different

Antigravity's [published hook contract](https://antigravity.google/docs/hooks)
permits exactly this for `PreToolUse`:

```json
{"decision": "allow|deny|ask|force_ask", "reason": "…", "permissionOverrides": []}
```

There is **no field for modified arguments**. And `PostToolUse` has exactly one
legal output:

```json
{}
```

So neither *tool* gate can alter a tool's input or its result. (The host can
still inject context at other points — `PreInvocation` does exactly that, below —
but nothing can bound a tool call.) Two consequences:

**The birth gate denies instead of rewriting.** A flooding command is refused
with a reason naming the contained form:

```
{"decision": "deny",
 "reason": "CTX_CONTEXT_GUARD: routed through ctx for bounded capture. Re-run it as: ctx run -- pytest -q"}
```

Containment holds — the flood never happens — but it costs one turn while the
agent re-issues the command itself, and the refusal is visible where on other
hosts it would be silent.

**There is no output-side safety net at all.** If something gets past the birth
gate, nothing downstream can shrink its result.

To be precise about what the birth gate *does* cover on this host, because it is
more than shell commands — the `PreToolUse` hook matches `run_command`, the file
readers (`Read`/`read_file`/`view_file`), the directory and search tools
(`list_dir`, `grep_search`, `find_by_name`, `glob_search`, `codebase_search`) and
the edit tools. An oversized file read is caught, not just a noisy test run.

The gap is **MCP and connector results**. They are not a command the birth gate
can inspect and bound ahead of time, so a verbose connector response lands in
your transcript in full and nothing can trim it afterwards.

> **If you use Antigravity, this is the one thing to know:** birth-gate coverage
> carries all the weight. Retrieve through the bounded `ctx` MCP tool
> (`ctx search` / `get` / `stats`), which is capped by construction, rather than
> through connectors that return unbounded payloads.

Antigravity also has **no `SessionStart` event**. The pre-flight capability-surface
advisory rides `PreInvocation` instead, as an `injectSteps` *ephemeral* message —
ephemeral because `PreInvocation` fires before every model call, so a persistent
message would re-accumulate context on each one.

## After-the-fact persistence is not containment

An important distinction: Antigravity's birth-gate denial executes nothing and
captures nothing. Once the agent reissues the named `ctx run`, capture happens
normally. If an over-budget result instead reaches Antigravity's post-tool hook,
straitjacket can persist it but cannot replace what the model sees. The raw bytes
reach the transcript *and* the store, so afterwards you can still do:

```bash
ctx get run:7a139fe6ef06#stdout --lines 1:3
```

The flood is not prevented, but the evidence is addressable rather than lost.

This is why **`ctx gain` reports differently on that host**. Where a digest was
substituted, it books the saving. Where it was only stored, it books the event at
raw→raw:

```
contained: 273.4 KiB raw -> 273.4 KiB emitted (1.0x)
est tokens kept out of context: 0
```

That is deliberate. A containment ledger that credited a saving which never
happened would be worse than no ledger — you would budget against fiction.

**This does not mean `ctx gain` is always 1.0× on Antigravity.** You will see a
mix. When the birth gate denies a command and the agent re-issues it as
`ctx run -- …`, that capture is real containment and books a real saving. It is
only the results captured *after the fact* — the ones nothing could substitute —
that book raw→raw. A 1.0× line is therefore a useful signal rather than a bug
report: it tells you that specific payload got past the birth gate, which is your
cue to route it through `ctx` explicitly or to retrieve it via the bounded MCP
tool.

## `antigravity-sdk` — the headless alternative

`agy`, the official CLI, has a second limitation: it authenticates by
**interactive OAuth browser login** and ignores `GEMINI_API_KEY`, so it cannot
run unattended — not in CI, not from cron.

`ctx orchestrate` treats that limitation as a hard routing capability. Automatic
assignment, escalation, and coordination exclude `antigravity`; an explicit
host pin can still select it for an attended run. Use `antigravity-sdk` when the
same Gemini route must run headlessly — see
[Routing](ROUTING.md#pinning-a-host-or-model).

`antigravity-sdk` is ctx's own agent built on the `google-antigravity` SDK. It is
a **separate host**, not a replacement: your `agy` install is untouched, and both
appear in `ctx wrap detect` with their own capabilities.

```bash
ctx wrap antigravity-sdk   # ctx builds and owns the venv (~40s)
```

It is headless (`GEMINI_API_KEY`) and it has **both gates**, because ctx owns the
tool implementations: the flooding builtins (`RUN_COMMAND`, `VIEW_FILE`,
`SEARCH_DIR`, `LIST_DIR`, `FIND_FILE`) are disabled and replaced with ctx-backed
equivalents that return bounded output by construction. Nothing needs to be
substituted afterwards because nothing was ever unbounded.

Trade-off worth stating: it is *our* agent, not Google's. When Antigravity ships
a feature, this shim does not have it. Use `agy` for interactive work; use
`antigravity-sdk` when something needs to run unattended.

## Host prefix bytes are the host's — and they are audited

Every byte the harness injects into a prompt is locked behind the prefix
manifest (`ctx.prefixassets`, ~3.5 KB resident). That manifest cannot see the
bytes the **host** adds because of the harness: a wrapper flag that flips a host
setting can add tens of KB to every request without touching one manifest
asset. That happened. Claude Code treats any non-Anthropic `ANTHROPIC_BASE_URL`
as a gateway that may not forward its `tool_reference` beta and turns deferred
tool loading off; `ctx wrap claude --proxy` set exactly such a URL, so the
prompt carried 41 inline tool schemas instead of 16, about 15k cached tokens on
every one of ~120 calls per session, and `ctx gain` reported the harness as
876 tokens. The DeepSWE receipt (`evals/agentbench/deepswe-2026-09-13.md`)
measured it as 82% of a 27% cost regression.

Three mechanisms now cover that class, from cheapest to most decisive:

| mechanism | cost | what it catches |
|---|---|---|
| **Wire prefix audit.** The observer proxy records, per request, the system-prompt bytes, tool count, tool-catalogue bytes and whether a deferral marker (`ToolSearch`, or `defer_loading`) is present; `window.json` keeps the first, the scorecard prints a `prefix:` line and flags `⚠ prefix tax` when a long catalogue has no deferral marker. | free, every proxied session | a host that inlines its catalogue, from the first scorecard of the first session |
| **Prefix parity probe.** `ctx wrap claude --probe-prefix` runs one naive single-turn session and one wrapped one (isolated `CLAUDE_CONFIG_DIR` each), reads the first request's prompt snapshot from both transcripts, and fails (exit 3) when the wrapped prefix exceeds the naive one by more than the manifest declares plus 4 KB slack, or when deferral was lost. `evals/agentbench/harness.py --prefix-parity` runs it before any paid arm and stores the verdict in the payload. | two calls to the cheapest model, about one cent | any wrapper-induced host prefix growth, whether or not it has a name yet |
| **Emission parity invariant.** `tests/test_run_passthrough.py` pins that a rewritten command whose output fits the inline budget emits at most the native bytes plus one handle line, with the command's own exit status. | free, in CI | receipt-shaped tool results (measured at 2-8x on sub-KB outputs) |

The rule these encode: the harness's cost to a session is measured **on the
wire, against a naive control**, never inferred from the bytes the harness
knows it wrote.

## Checking your own install

```bash
ctx wrap detect     # which hosts are installed, and their models/prices
ctx doctor          # is the harness actually wired up here
```

`ctx setup` runs these configuration checks and tells you if one fails. A green
configuration check is not a substitute for the still-pending live Codex hook
receipt.

## Where the truth lives

If you are extending this rather than operating it: the capabilities are
declared on `HostSpec` (`input_substitution` / `output_substitution`) in
`src/ctx/hosts.py`, and the hot path branches on `DIALECT_CAPS` in
`src/ctx/hook.py`. They are duplicated deliberately — the hook path has a latency
contract that forbids importing the registry — and
`tests/test_dialect_conformance.py` fails if they ever disagree. That test exists
because this project shipped a version where they *did* disagree: the Antigravity
dialect was implemented against an assumed contract that the published one
contradicts. [ADR 005](../spec/adr/005-antigravity-hook-contract.md) records what
happened and how to re-check it.
