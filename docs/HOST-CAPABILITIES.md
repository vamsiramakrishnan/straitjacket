# Host capabilities — what each agent can actually enforce

Straitjacket is the sidecar; the hosts below are the coding harnesses. Each
integration connects the sidecar's evidence tools to the user's existing agent.
The optional `antigravity-sdk` entry is a separate ctx-owned agent runtime,
not a requirement for using the sidecar with Google's Antigravity CLI.

**Status:** current implementation and published host contracts. Claude Code
(2.1.322) and Antigravity (`agy` 1.5.5) have live receipts for the facts marked
"observed". The Codex gates are contract-tested from the vendor's documentation
(Codex CLI 0.167.1) and have not been run live. **Antigravity contract source:**
[`spec/adr/005-antigravity-hook-contract.md`](../spec/adr/005-antigravity-hook-contract.md),
with the 2026-11-11 update at the end of that page.

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
  full. Where a host cannot rewrite a particular tool, the call is denied and the
  refusal names the bounded command; nothing is captured until the agent reissues
  it.
- **The output gate** (`PostToolUse`) fires *after* a tool returns. It is the
  safety net: if something slipped past the birth gate, an oversized result is
  replaced by a bounded digest before the model sees it.

A host needs a specific API for each. The birth gate needs a way to *rewrite a
tool's arguments*; the output gate needs a way to *replace a tool's result*. Two
more points matter for pastes and sessions: a **prompt** hook
(`UserPromptSubmit`) and a **session start** hook. The section
[What ctx can do at each point](#what-ctx-can-do-at-each-point) lists all four
per host.

## What each host enforces

| host | birth gate | output gate | how |
|---|---|---|---|
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/claude.svg" width="24" height="24" alt=""> **claude** (Claude Code) | Able to rewrite; by default ctx does not, and runs no command differently (`steering = "auto"`) | ✅ replaces the result, in the tool's own result shape | `updatedInput` / `updatedToolOutput` |
| <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/agents/codex.svg" width="24" height="24" alt=""> **codex** (Codex CLI) | Rewrites withheld: commands run as written under Codex's own approval | 🧪 implemented + contract-tested | live CLI receipt pending |
| <img src="../assets/agents/antigravity.png" width="24" height="24" alt=""> **antigravity** (`agy` CLI) | ✅ rewrites by default (it cannot replace output) `run_command` and `read_file` in place when the payload carries `hookEventName` (added in `agy` 1.5.4; observed on 1.5.5 only); other tools **deny** and name the command | ❌ **none** | `overwrite`; see below |
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

On Claude Code and Codex, by default (`[guard] steering = "auto"`), ctx does not rewrite
or approve a shell command. You type `pytest -q`, the command reaches the host exactly
as written, the host decides about it exactly as it would without ctx, and ctx bounds
the output after the command runs: a large result is replaced by a digest with a
retrieval handle. On Claude Code the full output is read from the host's saved copy;
when that is not available the digest says its copy is partial and names
`ctx run -- <command>`. The cost is that the command itself is not captured at birth.
`steering = "rewrite"` opts in to capture at birth on Claude Code, with the caveats in
[Configuration](CONFIGURATION.md#steering-what-happens-to-a-shell-command). Antigravity
still rewrites by default, because it cannot replace output. Where a rewrite happens, the
original command is passed verbatim to the shell as `ctx run --passthrough --shell --
'<original>'`. The Codex implementation is contract-tested only; call it live only after
a version-pinned Codex run records both gates.

## What ctx can do at each point

A hook is a program the host runs at a fixed point and that answers in a fixed
shape. The table below states, for the three CLI hosts, what ctx can do at four
points. **Observed** means a live run of that version recorded it; **documented**
means it is taken from the vendor's documentation and has not been run here.

| Point | Claude Code 2.1.322 (observed unless marked) | Codex CLI 0.167.1 (documented, not observed) | Antigravity `agy` 1.5.5 (observed) |
|---|---|---|---|
| **Prompt** (user submits) | `UserPromptSubmit` can **add context** or **block** the prompt. It cannot replace or shorten it. Added context over 10,000 characters is delivered as a file path plus a preview of about 2 KB. A block makes no model call. | `UserPromptSubmit` can **block** the prompt or **add context**. Added context is capped near 2,500 tokens. | No prompt hook is registered. An oversized prompt reaches the model unmodified. |
| **Tool input** (before the call) | `PreToolUse` can **rewrite the input** (`updatedInput`). Context it adds reaches the model. | `PreToolUse` can **allow with a rewritten input**. `ask` is unsupported and becomes a deny. ctx withholds rewrites on Codex. | `PreToolUse` can **rewrite arguments**: `{"decision": "allow", "overwrite": {…}}` is merged into the call's arguments before it runs. An empty object is a **deny**. |
| **Tool output** (after the call) | `PostToolUse` can **replace a built-in tool's output**, but only with an object in that tool's own result shape. A plain string is silently ignored. | `PostToolUse` can **replace the output** by blocking with a reason. | `PostToolUse` accepts **only an empty object** and is **not given the tool's output**. Output cannot be replaced or captured. |
| **Session start** | A `SessionStart` hook returns context once (documented; not separately observed here). | A `SessionStart` hook exists and ctx registers it (documented). | A `SessionStart` stage exists. A message injected there stays in the conversation: a canary injected at `SessionStart` was obeyed on a later model call. ctx applies `SessionStart` when the payload carries `hookEventName` (added in 1.5.4 per its changelog); observed on 1.5.5 only. |

Other facts from the same runs:

- **Claude Code, settings.** `skillOverrides` passed through `--settings` is
  honoured and merged key by key with the settings the user already has.
- **Claude Code, `Read`.** The `Read` tool refuses files with binary extensions
  before any hook runs, and a rejection of that kind fires no failure hook, so no ctx
  hook sees a read of a video or audio file. The `ctx view` hint for a video therefore
  rides the model's next `ffmpeg` or `ffprobe` shell command on that file. Reads of an
  image extension do reach `PreToolUse`, where ctx adds a note (see
  [Pastes and media](PASTES-AND-MEDIA.md#the-agent-opens-an-image-or-video-with-the-hosts-own-tool)).
- **Claude Code, permissions.** Observed on Claude Code 2.1.322: a hook's `allow`
  together with a rewritten input overrides the user's `deny` and `ask` rules, and a plain
  `allow` skips the default prompt for a call no rule covers (an MCP tool). A hook that
  returns no decision leaves the host to evaluate its rules, against the rewritten
  command only. What ctx does about it: it never approves a rewritten command, and it
  sends an `allow` only for its own commands, so that the follow-up commands a digest
  prints run without a prompt: always for those that read only ctx's store by a `run:`
  handle (`get`, `search`, `stats`, `diff`, `intake --chunks`, `view run:…`); only when
  no `Read` deny or ask rule and no outside-directory block is visible in any settings
  source, and nothing leaves the workspace, for `view <file>`, `get`/`search`/`stats` on
  paths, `q`, `map`, `def`, `refs` and the `blob:`, `snapshot:`, `search:` and
  `checkpoint:` handles; never for `run`, other `intake` forms or anything that writes. A `Read` rule in a source ctx cannot read (managed policy) does not
  bind those verbs; deny `Bash(ctx:*)` and `mcp__ctx__*` there if that matters. Upgrading
  consequence: commands an earlier ctx approved silently now go through Claude Code's own
  prompts, as they do without ctx. If you want those prompts gone, add your own allow
  rules for the original commands. Details:
  [Configuration](CONFIGURATION.md#steering-what-happens-to-a-shell-command).
- **Antigravity, permissions.** The host checks its permission rules against the
  *rewritten* command. `ctx.agyperm` accounts for that; see below.
- **Codex.** Hooks that are not managed require the user to trust the project
  before they run. Hosted tools (web search) do not use the local hook path, and
  `write_stdin` polls do not rerun `PreToolUse`.

What each point lets ctx do:

| Goal | Claude Code | Codex | Antigravity |
|---|---|---|---|
| Keep an oversized paste out of the model's context | Only by `block`: the prompt is stopped and the user resends it with a handle. `advise` cannot remove the paste. | Same, from documentation. | No prompt hook. |
| Store a paste and give the model a handle | Yes: `advise` mode (the paste still reaches the model this turn). | Same, from documentation. | No. |
| Bound a flooding command before it runs | Only with `steering = "rewrite"`; by default the command runs as written and the output is bounded afterwards. | No: rewrites are withheld; the output is bounded afterwards (documented, not run live). | Yes for `run_command` and `read_file`, by rewrite; other tools are denied with the command named. |
| Replace an oversized result | Yes, for a built-in tool only in its own result shape; if the digest does not fit the shape within budget, nothing is replaced. | Yes, by block with a reason (not run live). | No. |
| Tell the model what an image read will deliver, and how to get a region | Yes, a note on `Read` of a large image; the video hint comes with a later shell command. | Yes for `view_image` (contract only; nothing run). | No. |
| Add a once-per-session advisory | Yes, at `SessionStart`. | Yes, at `SessionStart` (documented). | Yes, at `SessionStart` on 1.5.5. |

## Configuration validation

Codex/OpenCode setup uses the same parsed edit plan for preview, application,
repair and owned removal. This changes configuration handling, not the host's
enforcement capabilities or live-validation status above. See
[Getting started](GETTING-STARTED.md) for commands and conflict handling.

Doctor validates Codex's bounded stdio command/arguments, `features.hooks`,
all lifecycle registrations (including `SessionStart`) and its instruction
block. OpenCode checks the selected JSON/JSONC MCP entry and native plugin
separately. A surviving ownership record lets doctor report missing components
when an integration was partially deleted. These are configuration checks;
they do not establish that a running host has loaded its plugin or hooks.

## Codex containment and batch recovery

Codex's output adapter uses `decision: "block"` to replace oversized output.
Some tool runners expose this as an exception or "Script failed", even though
the tool returned data. Successful capture now starts with
`[ctx output-contained] capture=stored`, followed by a retrieval handle.
`tool_error=reported_error` preserves an explicit tool error flag;
`tool_error=not_reported` means no error flag was supplied, not proof that a
website or command succeeded. Inspect the captured result to determine that.
Capture failures remain labelled `gate-failed` and do not claim stored output.

Retrieve the existing result before retrying the tool. In a batch, check which
calls completed before resuming; never replay mutations merely because the
wrapper reported an exception. JavaScript callers can use `Promise.allSettled`
for independent calls so one containment exception does not discard other
results. Dependent calls must wait for the retrieved result. The hook cannot
change the host runner's exception semantics.

## Which tool calls the Claude Code hooks see

Which tool takes which route is one declared table, `src/ctx/governance.py`, and
[`ctx coverage`](CLI.md#see-what-ctx-governs-ctx-coverage) prints it. The Claude
Code registrations are derived from it and pinned to it by tests:

- `PreToolUse` matches exactly the tools with an input-side route: `Bash`, `Read`,
  `Grep`, `Glob`, `Edit`, `Write`, `MultiEdit` and `NotebookEdit`.
- `PostToolUse` matches every tool *except* the exempt ones (session control and
  bookkeeping tools, and `ToolSearch`, whose result the host parses to load
  deferred tools), using a negative lookahead. A built-in added in a later release
  therefore falls to the output gate instead of to nothing. The sub-agent tool is
  governed under both of its names, `Agent` and `Task`, and the edit tools are
  included; an older registration listed `Task` only and omitted the edit tools.
- `PostToolUseFailure` records failed edits and the size of failure output. It can
  add context but cannot replace a result, so failure output is observed, never
  digested.
- MCP tools are recognised before any native name, by the `mcp__` prefix or the
  host's `mcp_server` field, and are governed by the output gate. An earlier
  classification by name treated a tool such as `mcp__github__list_issues` as a
  native search and denied it on hosts whose `PreToolUse` sees MCP names.
- A shell command counts as a ctx command, and is exempt from both gates, only when
  the whole command is one ctx invocation of the ctx this hook runs as. Any
  unquoted sequencing, pipe, background, redirection, subshell, substitution or
  line break makes it an ordinary command, bounded like any other. An executable
  that is merely named `ctx`, and an MCP server whose name only begins with the
  same letters, are not exempt. Previously `ctx stats && cat big.log` skipped both
  gates.
- `ctx setup` updates a hook matcher it owns that is still on a previously shipped
  default, and leaves a customised or shared one alone with a one-line note.
  Ownership means the whole command is exactly the invocation the installer writes.

Elsewhere in the table, the Codex and Antigravity registrations are pinned the same
way. A tool name the table has never seen is not ungoverned: it falls to the output
gate where the host can replace output, and is observed elsewhere.

## Antigravity (`agy` 1.5.5)

Antigravity's [published hook page](https://antigravity.google/docs/hooks), read on
2026-07-25 and recorded in [ADR 005](../spec/adr/005-antigravity-hook-contract.md),
listed no field for modified arguments, and ctx was built to deny and name the
bounded command instead. That is no longer what happens. Observed on `agy` 1.5.5
(headless, 2026-11-11, receipt
[`evals/agy-rewrite-live-2026-11-11.md`](../evals/agy-rewrite-live-2026-11-11.md)):

- The hook payload nests the call as `toolCall: {name, args, id}`, next to
  `hookEventName`. Earlier ctx versions read only flat `tool_name`/`tool_input`
  fields, so on this host every call was allowed. Payloads are now normalized at
  every stage. An `agy` older than 1.5.4 sends no `hookEventName`; ctx treats it as
  the older contract and keeps deny-and-steer.
- `PreToolUse` answers `{"decision": "allow", "overwrite": {…}}`. The host merges
  `overwrite` into `toolCall.args` before the tool runs and tells the model "A
  pre-tool hook changed the arguments of this tool call before it ran." An empty
  object `{}` from `PreToolUse` is read as a **deny**, so ctx names a decision on
  every path.
- `PostToolUse` accepts only `{}` and its payload carries **no tool result**. The
  stage records the call and captures nothing (ledger decision `no_result`).
- `SessionStart` exists, and `PreInvocation` fires before every model call.
- The host evaluates its permission rules against the **rewritten** command.

**What ctx rewrites.** Only `run_command` and `read_file`, the two tools whose
`overwrite` was observed. A flooding `run_command` is rewritten to run once through
`ctx run`. In the receipt run, a command that printed 343,557 bytes reached the
model as a 988-byte result (the digest and the host's notice), with no extra turn,
and the model then fetched one line through the handle (348 bytes). A `read_file`
is bounded by overwriting its line range: the requested start is kept and the
requested end is never passed on. Every other read or search tool (`view_file`,
`grep_search`, `find_by_name`, `list_dir`, `glob_search`, `codebase_search`) is
denied with the contained command named, because its `overwrite` could not be
observed. The edit tools are observed and never changed. `grep_search`, `find_by_name` and `list_dir` left the default toolset in `agy`
1.2.7, so the model searches through `run_command`, which the command route
governs.

```json
{"decision": "allow",
 "overwrite": {"CommandLine": "ctx run --passthrough -- python3 diagnose.py"}}
```

**Permissions are kept, not widened.** Because the host checks its rules on the
rewritten line, a rule that approves `ctx` would approve every rewrite, and a deny
rule on the original program would be bypassed. Both were confirmed live before the
fix. `ctx.agyperm` reads the user's rules and judges the *original* command. It
answers `deny` when a deny rule could match anywhere in the line, `force_ask` when
an ask rule could match or anything is beyond what it models, and `allow` only when
it can prove the host would have run the original, or when no rule could approve the
rewritten line so the host asks about `ctx run …` itself. A rewrite is therefore
never more permitted than the original command. The price is questions: a headless
`agy` refuses a `force_ask`.

Known limits of that check:

- Enterprise-managed rules are not visible to ctx. They can only add deny or ask
  rules, which the host still applies to the rewritten command.
- A deny word anywhere in a line is a deny, which is stricter than the host.
- Rules passed inline at launch, an `unsandboxed(...)` rule, an unreadable or
  oversized rules file (over 256 KiB), or a command over 4,096 characters give
  `force_ask`.
- A per-project rules file cannot be tied to the active project, so its allow
  rules prove nothing.
- Whether the host applies the `overwrite` before showing an interactive `force_ask`
  prompt was not observed.

**Tool output.** Nothing can replace or capture a tool result on this host. Output
is bounded only by rewriting the command before it runs. So the classifier names
the common read-only text tools (`nl`, `fold`, `fmt`, `column`, `paste`, `comm`,
`seq`, `sort`, `cut`, `uniq`, `base64`, `rev`, `expand`, `unexpand`) and the git
listings (`ls-files`, `ls-tree -r`, `rev-list`, `grep`, `for-each-ref`, `ls-remote`,
`show-ref`) and rewrites them like `cat` when they run on their own, as it does `ls`
with a glob operand. Before, the text tools were unknown and asked (a headless run
ended with no answer) and the listings ran natively: in a replay of 182 labelled
commands, floods run natively went from 5 to 0. Forms bounded
by their arguments stay native (`rev-list --count`, `rev-list -n 5`, `grep -q` without `-e`/`-f`,
`for-each-ref --count=5`, `show-ref --verify`, a non-recursive `ls-tree`; a count of 0
is no bound). A text tool is judged as an unknown command when it writes a file or
runs a program, takes its operands from a file, is not the bare tool (`./sort`, an
environment prefix), names a glob or `~user` operand, or reads outside the
workspace or a device (links resolved); `seq` without options is sized by
its arguments (native up to the head/tail cap, captured up to a million lines). As
parts of a pipeline these programs keep their earlier verdicts, except that a plain
`base64` is now vouched like the other text tools (asks: 13 to 2). A result from a tool that
is not rewritten (an MCP tool, `search_web`, a sub-agent) reaches the transcript
whole; a connector that returns a large payload is the gap. If a `PostToolUse` payload
does carry a result, ctx stores it so it keeps an address; it still cannot change
what the model sees. On 1.5.5 none is carried. The mitigation is
unchanged: retrieve through the bounded `ctx` MCP tool (`ctx search`, `get`,
`stats`), which is capped by construction, rather than through connectors that
return unbounded payloads.

**Session start and advisories.** The once-per-session capability advisory now rides
`SessionStart`, as `{"injectSteps": [{"systemMessage": {"systemMessage": …}}]}`, which
persists in the conversation. `PreInvocation` carries only what the relay has
pending, as an ephemeral message, because it fires before every model call. An
`agy` that sends no `hookEventName` keeps the older behaviour, with the advisory on
`PreInvocation`.

**Plugin loading.** On 1.5.5 the shipped plugin was not loaded at all: `agy plugin
validate` rejected `hooks.json` because one event nested its handlers, and the
loader skips a rejected file whole. That is fixed, `SessionStart` is registered, and
`agy -p /hooks --output-format json` lists the four ctx hooks (`SessionStart`,
`PreInvocation`, `PreToolUse`, `PostToolUse`) as loaded. `agy plugin validate` still
reports that `agents/ctx-explorer.md` is skipped, because its `tools:` frontmatter is
a string and names Claude Code tools.

**Replayed payloads.** `tests/test_agy_replay.py` replays sanitized copies of real
captured payloads (`tests/fixtures/antigravity/agy-1.5.5/`) for every event through
the real hook entry point, and fails when the keys the hook reads drift from them.

What this host still cannot do, stated once: it has no prompt hook, no output
replacement, and no capture of a result it did not rewrite.

## `antigravity-sdk` — the headless alternative

`ctx orchestrate` declares `agy`, the official CLI, as a host that is not run
unattended (`unattended=False` in `src/ctx/hosts.py`), so automatic assignment,
escalation, and coordination exclude `antigravity`; an explicit host pin can still
select it. This page used to give the reason as interactive OAuth browser login
with `GEMINI_API_KEY` ignored. That does not match `agy` 1.5.5, where a headless
`agy -p … --output-format json` authenticated with `GEMINI_API_KEY` and ran to
completion (observed; the [receipt](../evals/agy-rewrite-live-2026-11-11.md) used
it). The routing declaration has not been re-evaluated against that. Use
`antigravity-sdk` when the same Gemini route must run headlessly under ctx's own
orchestration — see [Routing](ROUTING.md#pinning-a-host-or-model).

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
ctx coverage        # which tool calls each host's hooks govern, and what they observed
```

`ctx setup` runs these configuration checks and tells you if one fails. A green
configuration check is not a substitute for the still-pending live Codex hook
receipt.

## Where the truth lives

If you are extending this rather than operating it: the capabilities are
declared on `HostSpec` (`input_substitution` / `output_substitution`) in
`src/ctx/hosts.py`, and the hot path branches on `DIALECT_CAPS` in
`src/ctx/hook.py`. Which tool takes which route is the table in
`src/ctx/governance.py`, which also names the tools a host may rewrite
(`REWRITABLE`: on Antigravity, `run_command` and `read_file`). They are duplicated
deliberately — the hook path has a latency contract that forbids importing the
registry — and `tests/test_dialect_conformance.py` fails if they ever disagree.
That test exists because this project shipped a version where they *did* disagree:
the Antigravity dialect was implemented against an assumed contract that the
published one contradicts. [ADR 005](../spec/adr/005-antigravity-hook-contract.md)
records what happened and how to re-check it, including the 2026-11-11 update.
