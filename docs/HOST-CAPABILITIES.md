# Host capabilities — what each agent can actually enforce

**Status:** current behaviour · **Normative source:**
[`spec/adr/005-antigravity-hook-contract.md`](../spec/adr/005-antigravity-hook-contract.md)

> **New here?** `ctx` is straitjacket's command — the project is straitjacket,
> the binary is `ctx`. If `ctx run`, `ctx wrap` or "digest" are unfamiliar, read
> [How it works](HOW-IT-WORKS.md) first: ten minutes, one command walked through
> the whole system.

straitjacket harnesses four hosts, and they do **not** all protect you equally.
The difference is not effort or polish — it is what each host's published hook
contract permits. This page tells you what you actually get on the host you use,
and what to do where the guarantee is weaker.

## The two gates

Containment happens at two points:

- **The birth gate** (`PreToolUse`) fires *before* a command runs. It is the
  primary mechanism: a flooding command is contained at source, so the unbounded
  output never enters the transcript. (It is still captured *in full* to the
  local artifact store — containment is about your context window, not about
  discarding data. See [Capture happens even where substitution
  cannot](#capture-happens-even-where-substitution-cannot).)
- **The output gate** (`PostToolUse`) fires *after* a tool returns. It is the
  safety net: if something slipped past the birth gate, an oversized result is
  replaced by a bounded digest before the model sees it.

A host needs a specific API for each. The birth gate needs a way to *rewrite a
tool's arguments*; the output gate needs a way to *replace a tool's result*.

## What each host enforces

| host | birth gate | output gate | how |
|---|---|---|---|
| **claude** (Claude Code) | ✅ rewrites transparently | ✅ replaces the result | `updatedInput` / `updatedToolOutput` |
| **codex** (Codex CLI) | ✅ rewrites transparently | ✅ replaces the result | `updatedInput` / `decision:block` |
| **antigravity** (`agy` CLI) | ⚠️ **denies** and names the command | ❌ **none** | see below |
| **antigravity-sdk** (ctx's own agent) | ✅ bounded inside the tool | ✅ bounded inside the tool | see below |

<picture>
  <source media="(max-width: 640px) and (prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-host-lanes-mobile.svg">
  <source media="(max-width: 640px)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-host-lanes-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-host-lanes.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-host-lanes-light.svg" width="100%" alt="Host enforcement lanes. Claude Code and Codex rewrite noisy commands and replace oversized output. Antigravity denies known command floods and the agent must re-issue ctx run on the next turn; connector output can only be persisted and observed. The ctx-owned Antigravity SDK uses bounded tools by construction.">
</picture>

On Claude Code and Codex, containment is invisible: you type `pytest -q`, the
hook silently substitutes `ctx run -- pytest -q`, and the agent never sees a
refusal.

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

## Capture happens even where substitution cannot

An important distinction: **the output gate not being able to substitute does not
mean nothing happens.** On every host, an over-budget result is still persisted
to the artifact store and keeps a retrieval address. On Antigravity the raw bytes
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

## Checking your own install

```bash
ctx wrap detect     # which hosts are installed, and their models/prices
ctx doctor          # is the harness actually wired up here
```

`ctx setup` verifies itself with these same checks and tells you if one
fails.

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
