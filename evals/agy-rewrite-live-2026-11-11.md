# Antigravity CLI: argument rewrite instead of deny, live

Date: 2026-11-11. Host: Google Antigravity CLI `agy` **1.5.5**, headless
(`agy -p ... --output-format json`), authenticated with `GEMINI_API_KEY`.
Model: `gemini-3.6-flash-low` (receipt runs); probes used
`gemini-3.6-flash-medium`. Script: [`agy_rewrite_live.py`](agy_rewrite_live.py).
Record: [`agy-rewrite-live-2026-11-11.json`](agy-rewrite-live-2026-11-11.json)
(scratch paths replaced by `<out>`, `<venv>`, `<scratch>`; no key).

## Result

On agy 1.5.5 the PreToolUse hook now rewrites a flooding shell command in
place. `python3 diagnose.py` printed **343,557 bytes** (4,000 lines). The
hook sent `{"decision": "allow", "overwrite": {"CommandLine": "ctx run
--passthrough -- python3 diagnose.py"}}`, the command ran once through
`ctx run`, and the model received a **988-byte** tool result (digest plus the
host's own notice "A pre-tool hook changed the arguments of this tool call
before it ran. Changed: CommandLine."). It needed no extra turn, and the
model did not have to cooperate. Before this change the same call was denied
with "Re-run it as: ctx run ...", which cost a model turn and relied on the
model re-issuing the command.

The model then used the handle from the digest to fetch the one line it
needed: `ctx get run:ee4d31c42b2a#stdout --lines 2500:2500` returned 348
bytes containing `request=a6a0845258a4`. That is the expected value,
recomputed independently from the fixture.

| | bytes |
|---|--:|
| raw stdout of `python3 diagnose.py` (stored whole in the ctx store) | 343,557 |
| tool result the model received for it (rewritten) | 988 |
| tool result for the follow-up `ctx get ... --lines 2500:2500` | 348 |

## Setup

Each session gets a throwaway `HOME`, a throwaway ctx state directory
(`CTX_STATE_HOME`) and a fresh repository. The plugin is installed with the
product's own path, `ctx wrap antigravity`. Permissions are granted only by
allow rules in `<HOME>/.gemini/antigravity-cli/settings.json`; no
permission-bypass flag is used.

```bash
python evals/agy_rewrite_live.py --agy ~/.local/bin/agy --out evals/_runs/agy-rewrite --dry   # no model call
python evals/agy_rewrite_live.py --agy ~/.local/bin/agy --out evals/_runs/agy-rewrite --sessions s1,s2
python evals/agy_rewrite_live.py --agy ~/.local/bin/agy --out evals/_runs/agy-rewrite --sessions s3
python evals/agy_rewrite_live.py --agy ~/.local/bin/agy --out evals/_runs/agy-rewrite-2 --sessions s4,s5
```

Before any model call, two free checks confirmed the install:

- `agy -p /hooks --output-format json` answers without a model call. It
  listed all four ctx hooks as loaded: SessionStart, PreInvocation,
  PreToolUse and PostToolUse.
- `agy plugin validate .agents/plugins/ctx-harness --json` reports one
  remaining error: the bundled `agents/ctx-explorer.md` frontmatter gives
  `tools: Bash, Read, Grep, Glob` as a string, and agy wants a list, so
  "the loader skips this agent". That file is golden-hashed in
  `src/ctx/prefix-manifest.json` and was left alone; see "Not fixed here".

Before this change the plugin's `hooks.json` was **rejected whole** by agy
1.5.5 (`invalid hook "ctx-harness": command hook must specify 'command'. The
loader skips this whole file`). For events without a matcher (PreInvocation,
SessionStart), agy takes a flat list of handlers, and the plugin had nested
its handlers under `"hooks"`. So until this change, no ctx hook ran on
current agy at all. The validator also flagged `Read`, `Edit` and
`MultiEdit` as Claude Code names that never match, and those were dropped
from the matchers and from the governance table.

## Observations

### a, b: rewrite, digest, retrieval (session s1)

Allow rules: `command(python3 diagnose.py)`, `command(ctx)`,
`command(grep)`, `read_url(127.0.0.1)`. The prompt (in the record) asked the
model to run the command, report the request id on output line 2500, run
`grep -rn NEEDLE .`, and fetch a local URL.

The digest the model received was `profile=logtemplate/v1`, "4,000 lines ·
335.5 KiB · est 85,889 tokens". It found 2 templates and surfaced the one
anomalous line (`L1717 ... region=apac gateway=legacy`) as exceptional. It
also printed `next: ctx get run:ee4d31c42b2a#stdout --lines 1717:1720`. The
model used that handle form for line 2500 on its next turn.

### c: search (session s1)

agy 1.5.5's default toolset no longer has `grep_search`, `find_by_name` or
`list_dir`; its changelog says they were retired from the default agent in
1.2.7. So the model searched with `run_command`, and the command route
rewrote `grep -rn NEEDLE .` in place, which is what the table says. The
rewrite in this workspace was `ctx q 'refs NEEDLE | group file'`, a
symbol-index query, and it returned **0 rows** (`0 rows after stage 1
(refs): engine: ast (textual)`). Plain `grep` would have found
`notes.txt:1`. So the rewrite was delivered and governed as declared, but it
lost a plain-text match that the original command would have found. This is
existing grep-steering behaviour, not part of this change. It matters more
now because, on this host, the rewrite is applied silently instead of being
offered as a suggestion.

The model then tried `ctx search NEEDLE`, which failed with a usage error
(`patterns` required). It then improvised `python3 -c "..."`. That command
was rewritten too, but no allow rule covered `python3 -c`, so the hook sent
it as `force_ask` (see f). Headless agy refused it and the run ended with no
final answer: `denied_actions: [command]`. Step 4 (the URL fetch) was never
reached.

### d: `ctx coverage --host antigravity` after s1 (verbatim, observed part)

```
observed  (.ctx-session-reads/coverage.jsonl: 9 rows, session all, 1 session(s) on record; history is bounded to the current and one rotated generation)
  host         tool                               stage        route        calls      raw B emitted B  decisions
  antigravity  run_command                        pre          rewrite          5          0         0  allowed=2,rewritten=2,rewritten_asked=1
  antigravity  run_command                        post         observe          4          0         0  no_result=4
  most ungoverned bytes into context (observed, exempt, captured-not-replaced, failed, or a replacement the host ignores):
            0 B     4 calls  antigravity run_command  (no_result=4)
```

The five pre rows are: two commands rewritten into ctx (`python3
diagnose.py`, `grep`); two ctx commands the model wrote itself and that
passed untouched (`allowed`); and one rewrite sent as `force_ask`. The post
stage observes four calls and captures nothing (`no_result`), because agy
1.5.5 PostToolUse payloads carry no tool output. The bytes the model
actually received therefore come from the transcript, not the ledger.

### e: a large result from a tool the hook cannot rewrite (session s3)

The aim was a 129,354-byte page fetched with agy's URL tool. **That was not
demonstrated.** Asked to "fetch ... with your URL-reading tool", the model
had no URL tool in its default toolset (it reasoned that `read_file` was the
closest fit). It called the MCP `list_resources` tool, whose 198-byte result
reached it raw: nothing on this host can replace a result. It then tried to
load the ctx MCP server's tool schema with `read_file` on
`<HOME>/.gemini/antigravity-cli/mcp/ctx-harness_ctx-harness/ctx.json`. ctx's
outside-the-workspace rule answered `force_ask`, and headless agy refused it
(`denied_actions: [read_file]`).

Two consequences:

- That refusal would have made every MCP tool unusable in headless runs. It
  is fixed after this run: a read of exactly `<appDataDir>/mcp/<server>/<tool>.json`
  (the shape of agy's own files, from a live listing) now passes, but only
  after the secret-path check, and never for a name that looks like a key,
  token or credential. Other reads outside the workspace still ask. This fix
  is covered by replay tests, not a live run.
- What happens to a large result from a non-rewritable tool (an MCP tool,
  `search_web`, `read_url_content` where offered, a sub-agent) is stated
  rather than observed. PostToolUse can only answer `{}` and receives no
  result, so the whole result enters the transcript raw. The hook records
  the call (`no_result`) and captures nothing.

### f: permissions (probes B and C, sessions s1, s2, s4, s5)

The host checks its permission rules against the **rewritten** command. Two
probes established this; their hook outputs, tool results and usage are in
the record under `probes`:

- Probe B: `ls -la` rewritten to `echo ...` ran under an allow rule for
  `echo` only; `echo hi` rewritten to `pwd` was refused.
- Probe C: `echo one` rewritten to `pwd` was refused with "permission check
  failed for command "pwd"", even with `permissionOverrides:
  ["command(echo one)"]` in the response. That field did not redirect the
  check.

So a rule approving `ctx` would approve every rewritten command. `ctx.agyperm`
closes that gap without re-implementing agy's rule engine. It answers
`allow` only where it can prove agy would have run the original:

- nothing in any rules file could approve the rewritten line, so agy asks
  about `ctx run ...` itself; or
- a rule from the global files (CLI `settings.json`, shared `config.json`)
  equals the whole line, or the line is one plain command (plain words only,
  no option word, no wrapper or shell as the program, no assignment) matched
  by `command(*)`, a literal word prefix, or a token-for-token regex rule.

Any deny rule that could match anywhere in the line (any window of words,
inside wrappers, substitutions, subshells and chains) gives `deny`. An ask
rule that could match, an unreadable or oversized source, an
`unsandboxed(...)` rule, rules passed inline at launch, or a command over
4,096 characters gives `force_ask`, as does everything else. The
adversarial tests (`tests/test_agyperm.py`) cross the documented hard cases
with rule sets: 1,428 construct cases (none may be allowed), 720 deny cases
through wrappers and chains (all must be denied), and 90 ask cases.

Live, after the change:

| session | rules | command the model ran | hook answer | outcome |
|---|---|---|---|---|
| s4 | allow `ctx`, `timeout`; deny `cat` | `timeout 5 cat big.log` | `deny` (ledger `denied`) | refused: "tool call denied by pre-tool hook" |
| s5 | allow `ctx`, `cat` | `cat big.log $(touch marker.txt)` | `force_ask` + overwrite (ledger `rewritten_asked`) | refused headless (`denied_actions: [command]`); `marker.txt` not created |
| s2 | allow `ctx` | `python3 diagnose.py` | `force_ask` (ledger `rewritten_asked`) | refused headless |

Before this fix, the reviewer saw s4 run through ctx and s5 run silently and
create the file. s1 and s2 ran under the earlier version of the module. The
new one gives the same answers for their commands: `python3 diagnose.py`
under `command(python3 diagnose.py)` is proven, and `python3 -c "..."` is
asked. Not observed: whether agy applies the `overwrite` before showing an
interactive `force_ask` prompt. Headless runs refuse without recording a
step.

### SessionStart (probe D)

With no surface advisory or relay backlog pending, ctx's SessionStart and
PreInvocation answered `{}` in these runs. Probe D verified the shape ctx
now uses at SessionStart, `{"injectSteps":[{"systemMessage":{"systemMessage":
...}}]}`. The canary appeared as a `SYSTEM_MESSAGE` step, and the model's
answer on the *second* model call ended with the requested word ("The shell
command printed `hi`. PELICAN"). So a once-per-session advisory delivered at
SessionStart persists.

## Costs

| run | model | model calls | input tok (cached) | output tok (thinking) |
|---|---|--:|--:|--:|
| s1 | 3.6-flash-low | 5 | 24,256 (22,330) | 1,298 (1,019) |
| s2 | 3.6-flash-low | 1 | 7,906 (0) | 141 (96) |
| s3 | 3.6-flash-low | 2 | 16,441 (0) | 679 (553) |
| s4 | 3.6-flash-low | 2 | 16,097 (0) | 514 (434) |
| s5 | 3.6-flash-low | 1 | 7,905 (0) | 211 (165) |
| probe B | 3.6-flash-medium | 1 | 7,343 (0) | 2,825 (2,640) |
| probe C | 3.6-flash-medium | 1 | 7,303 (0) | 695 (547) |
| probe D | 3.6-flash-medium | 2 | 15,253 (0) | 676 (631) |

At the shipped list price for `gemini-3.6-flash` ($1.25/M input, $0.12/M
cached, $7.50/M output), the sessions cost **$0.087** and the probes
**$0.069**: 15 model calls in all. agy's `input_tokens` includes cached
tokens, and `output_tokens` includes thinking.

## Caveat on s1

s1's rewritten commands (`ctx run --passthrough -- ...`, `ctx q 'refs ...'`)
come from the shared shell rewriter. That rewriter is being replaced so the
original command string reaches the shell verbatim. s1's byte counts and its
search result should be re-run after that change lands.

## Not fixed here

- `plugins/antigravity/agents/ctx-explorer.md` is skipped by agy 1.5.5 (its
  `tools:` frontmatter is a string, and it names Claude Code tools). The file
  is golden-hashed prefix text.
- Steering `grep -rn <identifier>` to `ctx q 'refs ...'` drops plain-text
  matches (c).
- The MCP-schema read fix (e) is covered by replay tests, not a live run.
