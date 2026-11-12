<sub><a href="README.md">« straitjacket / docs</a></sub>

# Troubleshooting & FAQ

What to do when something doesn't work. Each entry is **symptom → cause → fix**.
If you're not sure where to start, run `ctx doctor` (add `--antigravity` if you
use that host) — it prints one `✓`/`✗` per check and its failures map onto the
sections below.

## Start here: read `ctx doctor`

```bash
ctx doctor                 # core checks: PATH, store, policy, hook classifier, engines
ctx doctor --antigravity   # also checks the Antigravity plugin files
```

The header reads `OK` or `PROBLEMS FOUND`, and each line is a named check. The
checks and what a `✗` means:

| Check | A `✗` means |
|---|---|
| `ctx on PATH` | `ctx` isn't on `PATH`. The hook uses an absolute path so capture still works, but type-`ctx` commands won't. |
| `store writable` | The artifact store can't be created or written — a permissions, disk, or path problem (see [Store errors](#the-store-wont-write)). |
| `hook classifier` | The guard's self-test failed: a known flood (`pytest -q`) didn't classify as expected. The classifier is misconfigured. |
| `manifest schema` | The latest run manifest failed schema validation — usually schema drift after an upgrade. |
| `search engine` / `ignore matching` | Informational: shows whether `ripgrep` / `pathspec` are present or the pure-Python fallback is in use. Never a hard failure. |
| `plugin …` (with `--antigravity`) | An Antigravity plugin file is missing or invalid — re-run `ctx antigravity install`. |
| `no duplicate installation` | Both the plugin and the standalone skill are installed; remove one. |

## Setup problems

### `ctx: <host> template not found; reinstall ctx-harness`

**Cause:** the packaged host templates (the plugin/hook files `ctx wrap` copies
in) can't be found next to your install. Usually a broken or partial install.
**Fix:** reinstall the package — `pip install -e .` from a clone, or reinstall
the wheel.

### `standalone skill already installed …; remove it first`

**Cause:** you have an older standalone skill at `.agents/skills/ctx-harness`
and are now installing the Antigravity **plugin**, which already contains the
skill. The two must not coexist.
**Fix:** remove `.agents/skills/ctx-harness`, then re-run setup. (`ctx doctor
--antigravity` reports this as the `no duplicate installation` check.)

### Codex or OpenCode: setup stopped with a configuration diff

Setup previews changes before editing existing user configuration. Review the
exact diff, then run `ctx setup --host codex --apply` or
`ctx setup --host opencode --apply` for safe additions. The same plan powers
`ctx setup --host <name> --preview`, repair and removal.

If setup names a conflict, `--apply` still refuses. Reconcile that exact entry
using the proposed diff: for example, decide whether to enable an explicitly
disabled hook or replace a different `ctx-harness` server. Comments and other
integrations are preserved. Invalid syntax and duplicate keys require repair
at the reported location before a structural diff can be generated.

### `ctx doctor`: "ctx launch form"

```text
  ✗ ctx launch form — `python -m ctx` without -P imports a `ctx` package from the workspace: .claude/settings.json (hooks.PreToolUse[0].hooks[0]): `ctx setup --host claude` rewrites it to the -P form
  ✗ ctx launch form — `python -m ctx` without -P imports a `ctx` package from the workspace: .mcp.json (mcpServers.mine): ctx does not manage this entry; set "command": "python3", "args": ["-P", "-m", "ctx", "mcp"]
```

**Cause:** an installed hook, status-line or MCP command starts ctx as
`python -m ctx` without `-P`. Python puts the current directory first on the import
path, so a repository with a top-level `ctx/` package or `ctx.py` would run its own
code as the guard on every tool call. Releases before the fix wrote that form into
host configuration whenever `ctx` was not on `PATH`, and used it for background jobs.
Doctor reads every file a ctx installer writes, plus the hosts' own user-scope
settings, and prints one row per finding. A passing row reads "no installed command
imports ctx from the working directory".

**Fix:** the row says which of two cases it is.

- **"`ctx setup --host <host>` rewrites it to the -P form"**: the command is one ctx
  wrote, in a file ctx manages. Run that setup command. It upgrades ctx-owned commands
  in place and prints the file it changed, for example "updated 1 ctx command(s) in
  .claude/settings.json from the unsafe `python -m ctx` form to `python -P -m ctx`".
  Run `ctx doctor` again.
- **"ctx does not manage this entry; …"** (or setup will not rewrite it, with the
  reason, such as a symlink or a read-only file): setup will not touch it. Apply the
  exact replacement the row prints by hand.

Existing installs should run `ctx setup` once and then `ctx doctor`. Setup refuses to
write through a symlink and names the file.

### Codex: `MCP client for ctx-harness failed to start: No such file or directory`

An older generated fallback put the whole `python -m ctx` invocation in MCP
`command`. Codex launches that field directly, without a shell. Preview the
repair with `ctx setup --host codex --preview`. The corrected entry separates
`command = "/path/to/python"` from `args = ["-P", "-m", "ctx", "mcp", "--bounded-only"]`
(`-P` keeps a `ctx` package in the repository from being imported).
Owned entries are repairable when they still match the recorded value. Legacy
entries without an ownership record need the shown correction reviewed and
integrated before setup proceeds; the old banner does not authorize overwrites.
Restart Codex afterward.

### Codex: `PreToolUse hook returned unsupported permissionDecision:allow`

**Cause:** an older hook emitted an explicit `allow` for ordinary pass-through
calls. Codex reserves that decision for input rewrites that also contain
`updatedInput`. **Fix:** upgrade `ctx-harness` and restart Codex. The current
hook emits `{}` for pass-through and for shell commands (rewrites are withheld on
Codex), and `deny` for blocked calls.

### `--workspace is not a directory: <path>` (exit 2)

**Cause:** the `--workspace` you passed doesn't point at a directory.
**Fix:** pass an existing directory, or omit `--workspace` and let straitjacket
resolve the root (see [Configuration](CONFIGURATION.md#where-configuration-lives)).

## The harness isn't doing anything

This is the most common report, and it almost always has a mundane cause.

### The guard is a complete no-op

**Cause:** `[guard] mode = "advisory"` turns the guard off — PreToolUse allows
everything unconditionally and the PostToolUse output gate replaces nothing.
**Fix:** set `mode = "guarded"` (the default) in `ctx.toml`. `"strict"` is accepted
but behaves exactly as `guarded`; `ctx coverage` says so when a workspace sets it.

Related: a **syntax error in `ctx.toml`** silently reverts settings to their
defaults rather than erroring, so an intended `mode = "advisory"` sitting under a
malformed line behaves as the default. Confirm the file parses. The one setting
that fails the other way is `[intake] mode`: an unreadable `ctx.toml` turns the
prompt gate off.

### A specific command isn't being captured

**Cause:** the guard matches tool names by substring on the lowercased name
(edit / write / command / read / grep / glob / list …). A tool whose name
matches none of these is passed through untouched.
**Fix:** for shell commands this is rarely the issue; if you've renamed or wrap
tools unusually, route the work through `ctx run -- <command>` explicitly.

### Native `Grep`/`Glob` results still flood

**Cause:** a host's built-in `Grep`/`Glob` tools bypass the shell path, so while
the guard can classify them, their *output* isn't captured or digested. **Fix:**
this is exactly why `ctx wrap` removes the native `Grep`/`Glob` tools under the
default `[guard] collapse` setting (which now governs only this) — make sure you set the host up with `ctx wrap` (not a
hand-rolled hook), and prefer `ctx search` / `ctx q` for repository search.

### A large-file Read shows fewer than 240 lines

**Not a bug.** The guard bounds a Read of a file over `max_inline_bytes` to
`max_inline_lines` lines; when those lines are themselves over
`max_tool_output_bytes` (long log lines), the output gate keeps the leading whole
lines that fit and adds a note with the range shown and the `offset` to continue
from. Earlier releases replaced such a Read with a JSON shape digest holding no
line of the file.

### The guard isn't confining paths

**Cause:** path confinement is only enforced once a workspace root resolves. If
the host passes several unrelated workspace paths and none matches, the root is
`None` and confinement is skipped. **Fix:** pass `--workspace <dir>` explicitly,
or run from inside the repo so the root resolves.

## Permission prompts you didn't expect

### "ctx used to run my commands without asking and now Claude Code prompts"

**Cause:** an earlier ctx answered `allow` together with its rewrite of a shell
command, and for commands it passed through. On Claude Code an `allow` together with a
rewritten command overrides your deny and ask rules, and a plain `allow` skips the
default prompt, so installing ctx approved shell commands (and MCP calls) you were
never asked about. ctx no longer does that. By
default (`[guard] steering = "auto"`) a shell command reaches Claude Code exactly as
written and the host asks as it would without ctx; ctx approves only its own commands that read
its store or the workspace (the follow-ups a digest prints), never `ctx run` or a
rewritten command. **Fix, two legitimate remedies:** add your own allow rules on the original
commands (for example `Bash(pytest:*)`) in your Claude Code settings; or set `steering
= "rewrite"` to capture at birth, accepting the caveats: the host's prompts and rules
then concern the `ctx run` wrapper, and a rule from a source ctx cannot read (managed
policy) does not see the original command. A third option, `ctx run -- <command>`
yourself, goes through the same prompts. `ctx doctor` prints a "claude code approval"
row stating the current behaviour.

### "secret-bearing path … requires an explicit permission step"

**Not a bug.** Reading a path that looks secret-bearing (`.env`, `.aws`, `.ssh`,
`*.pem`, `*.key`, `id_rsa`, `credentials`, `secrets` …) always force-asks and is
excluded from automatic capture — even under permissive steering. **Fix:**
confirm the prompt if you really intend it; there's no way to silence this by
config, by design.

### "container escape surface … requires an explicit user-visible permission step"

**Not a bug.** A `docker`/`podman`/`nerdctl` invocation that reaches past the
workspace confinement — `--privileged`, a host namespace (`--pid=host`,
`--network host`, `--ipc/--uts/--userns/--cgroupns=host`), `--device`,
`--cap-add SYS_ADMIN`/`SYS_PTRACE`/`ALL`, the engine socket, or a bind mount
from outside the workspace — always force-asks, through every door
(`> file 2>&1`, chain segments, `ctx run --`). Mounting the workspace itself
(`-v $PWD:/app`, `-v ./data:/data`) or a named volume does not trigger it.
**Fix:** confirm the prompt if you intend it; a mount from a shell variable
other than `$PWD` asks because the guard cannot resolve it — spell the path.

### "path resolves outside the active workspace"

**Cause:** the read resolves (after following symlinks and `..`) to somewhere
outside the workspace root. **Fix:** confirm the prompt, pass `--workspace` to
widen the root, or set `[workspace] allow_outside_root = true` if this is
routine for your setup.

### "unknown output bound for '`<prog>`'"

**Cause:** under `guarded` mode, a command the classifier doesn't recognize is
force-asked in case its output is large or it mutates state. This prompt comes from
Antigravity's guard; on Claude Code and Codex ctx no longer asks about an unknown
output bound, and the host decides about the command. **Fix:** confirm a genuinely
safe unknown command, or use `ctx run -- …` when it can flood. You can change `[guard]
unknown_command` (`allow` / `deny` / `ask` / `force_ask`), but a blanket `allow`
removes the review boundary for both unclassified reads and unclassified mutations.
See [Configuration](CONFIGURATION.md#steering-what-happens-to-a-shell-command).

### "in-place sed/awk" or "deeply nested shell" prompts

**Cause:** in-place edits (`sed -i`) and deeply nested shell invocations are
force-asked so you preview them first. **Fix:** confirm, or restructure — use
`ctx run --shell` for a genuine pipeline.

## The store won't write

**Symptom:** `ctx doctor`'s `store writable` check fails, or a command exits
with `ctx: <error>` mentioning the store.
**Cause:** the store directory can't be created or written. The store lives at
`$CTX_STATE_HOME`, else `$XDG_STATE_HOME/ctx`, else `~/.local/state/ctx`. A
wrong or unwritable override of those env vars is the usual culprit, followed by
a full disk or a permissions problem.
**Fix:** check the env vars point somewhere writable (or unset them to use the
default), verify free disk, and confirm directory permissions. Writes are atomic
(temp + fsync + rename), so a crash never leaves a partial artifact.

### Retrieval errors from the store

| Message | Meaning / fix |
|---|---|
| `id prefix too short … (need ≥6 hex chars)` | Use at least 6 hex characters of the handle. |
| `ambiguous short id '<x>'; candidates: …` | Two artifacts share that prefix — use a longer one. |
| `no object matches id prefix '<x>' in this workspace` | Wrong id, or you're in a different workspace than where it was captured. |
| `unknown span '<x>' …` | Span tokens come from a specific digest — re-run the digest, or retrieve with `--lines` coordinates instead. |

## The observer proxy

### "observer proxy failed to start; continuing without it"

**Not fatal — fail-open.** If the proxy doesn't bind within 5 seconds, the
session simply runs unproxied; nothing is broken, you just get no wire
measurements for it. **Fix:** re-run; if it persists, another process may hold
the port.

### `ctx stats --session` says "no wire observations"

**Cause:** the session wasn't run under the proxy. **Fix:** launch with
`ctx wrap claude --proxy …`. Wire-cost stats only exist when the observer was
attached.

### HTTP 502 `ctx proxy: upstream unreachable`

**Cause:** the proxy couldn't reach the upstream API after a retry. **Fix:** a
transient network/upstream problem — retry. The proxy binds loopback only and
never logs request bodies or auth headers.

### `ctx wrap claude` returns 127 / "claude not found on PATH"

**Cause:** the `claude` CLI isn't installed or isn't on `PATH`. **Fix:** install
it and re-run. If your `claude` build lacks `--settings`, straitjacket
transparently falls back to merging settings temporarily and restoring them on
exit (you'll see a one-line notice).

## FAQ

**Does straitjacket send my code or output anywhere?**
Capture and storage are local — an on-disk SQLite + blob store outside the repo.
Anything shown in the agent conversation, including digests and retrieved
excerpts, still reaches the configured model provider through the host's normal
request path. The optional observer proxy relays that existing traffic
byte-exact on loopback and records only usage/window metadata, never request
bodies or auth headers.

**Will it ever delete or rewrite my transcript history?**
It does not rewrite transcript history. Omitted content keeps a coordinate while
its artifact is retained. Artifacts are plaintext and remain until an explicit
`ctx gc`; once collected, their omitted bytes are no longer retrievable.

**Does it change task outcomes?**
Measured outcomes are mixed. Some suites reached parity with less resident
output; the small canary cost more and took longer, and one N=1 dogfood run
reproduced fewer failing test nodes in the wrapped arm. When output is small,
native execution can be better. See [Why straitjacket](WHY-STRAITJACKET.md) and
[`evals/`](../evals/).

**Do I need ripgrep / ctags / other binaries?**
No. They accelerate or enrich analysis, but every path has a pure-Python
fallback with the same output contract. `ctx doctor` shows which engine is
active.

**How do I set up just one host?**
For Hermes, OMP, OpenCode, and DSH, see
[Agent integrations](AGENT-INTEGRATIONS.md) for configuration conflicts,
profile selection, DSH's required launch patch, and removal. These integrations
do not intercept native tools; use `ctx run` explicitly for command capture.

`ctx wrap antigravity`, `ctx wrap claude`, `ctx wrap codex`, or
`ctx wrap antigravity-sdk` each set up exactly one. `ctx setup` detects
which agent CLIs you have and configures those, then verifies the result and
tells you what to run next; hosts it would have to *build* rather than detect
(`antigravity-sdk`) are offered, never configured implicitly.

**How do I preview what setup will write without touching anything?**
`ctx wrap <host> --print-config`.

**How do I run a one-off session without persistent host configuration?**
`ctx wrap claude -- -p "…"` injects host settings for that process only and
removes those settings on exit. Captured artifacts, the local ledger, and
telemetry may remain under the normal retention policy.

## How do I turn the harness off, or uninstall it?
For a single break-glass command, confirm the force-ask prompt. To disable
steering everywhere, set `[guard] mode = "advisory"` — that makes both the
PreToolUse guard and the PostToolUse digest gate allow everything, but the hooks
and the `ctx` MCP tool stay registered. The prompt gate has its own switch,
`[intake] mode = "off"`. To fully remove the host integration, delete the files
`ctx wrap` added (this is exactly what each host's setup output tells you):

For Codex and OpenCode installs created by the configuration planner, use
`ctx setup --host codex --host opencode --remove` to preview owned removal;
add `--apply` to execute it. Other integrations and unowned entries remain.
The following manual steps also cover older installs without ownership records.

- **Antigravity** — remove the `.agents/plugins/ctx-harness/` directory. In
  `~/.gemini/antigravity-cli/settings.json`, remove `statusLine` only when its
  command is the ctx status-line command; preserve a user-defined status line.
- **Claude Code** — remove the `ctx` hook entries from
  `.claude/settings.json`. Remove `statusLine` only when its command is the ctx
  status-line command. Remove `.claude/agents/ctx-explorer.md` only when setup
  reported that it created the file; a pre-existing file was left untouched.
  Remove the marker-delimited `ctx-harness` block from `CLAUDE.md`.
- **Codex** — remove the `ctx-harness` MCP table from `.codex/config.toml`, the
  `ctx` hook entries from `.codex/hooks.json`, and the marker-delimited
  `ctx-harness` block from `AGENTS.md`. Delete a whole config file only when
  setup created a fully managed file and it contains no unrelated settings.

`ctx.toml` and `.ctxignore` are workspace policy, not host registration. Remove
them separately only if the workspace no longer uses direct `ctx` commands.
Removing host configuration does not delete captured artifacts. Use `ctx gc`
with an explicit retention horizon when you intend to collect them.
The content-free `.ctx-session-reads/setup.json` readiness receipt may be
removed separately; do not remove other workspace-local store files as part of
host uninstall.

Ephemeral `ctx wrap <host> -- …` sessions restore their injected host settings
on exit. Captured artifacts, the local ledger, and telemetry may remain under
the normal retention policy.

**How do I reclaim disk?**
`ctx gc` mark-and-sweeps expired artifacts (retention is `[store] retention_days`,
default 30); `ctx pin` protects an artifact from collection.

**Why does doctor say `workspace-local fallback`?**
The configured user-state directory rejected a real write (common in managed
sandboxes), so ctx selected `.ctx-session-reads/store`. The choice is sticky to
keep every emitted `run:`/`blob:` handle retrievable across later commands.
This is healthy, not a degraded capture mode. Delete
`.ctx-session-reads/store-backend.json` only when the user-state directory is
writable again and you intentionally accept starting a separate store lineage.

**Where do I change budgets, scopes, or redaction?**
All in `ctx.toml` — see the [Configuration reference](CONFIGURATION.md).

---

[Getting started](GETTING-STARTED.md) · [Configuration](CONFIGURATION.md) · [CLI guide](CLI.md) · [Concepts](CONCEPTS.md)
