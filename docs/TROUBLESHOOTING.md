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

### Codex: setup printed a snippet instead of editing `.codex/config.toml`

**Not a failure — by design.** straitjacket never rewrites an existing
`.codex/config.toml` in place (editing a TOML with duplicate tables is a
data-loss hazard). If the file exists but doesn't yet register `ctx-harness`,
setup prints the exact `[mcp_servers.ctx-harness]` snippet for you to paste.
Add it and you're done.

### `--workspace is not a directory: <path>` (exit 2)

**Cause:** the `--workspace` you passed doesn't point at a directory.
**Fix:** pass an existing directory, or omit `--workspace` and let straitjacket
resolve the root (see [Configuration](CONFIGURATION.md#where-configuration-lives)).

## The harness isn't doing anything

This is the most common report, and it almost always has a mundane cause.

### The guard is a complete no-op

**Cause:** `[guard] mode = "advisory"` turns the guard off — it allows
everything unconditionally. **Fix:** set `mode = "guarded"` (the default) or
`"strict"` in `ctx.toml`.

Related: a **syntax error in `ctx.toml`** silently reverts settings to their
defaults rather than erroring, so an intended `mode = "strict"` sitting under a
malformed line behaves as the default. Confirm the file parses.

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
default collapse setting — make sure you set the host up with `ctx wrap` (not a
hand-rolled hook), and prefer `ctx search` / `ctx q` for repository search.

### The guard isn't confining paths

**Cause:** path confinement is only enforced once a workspace root resolves. If
the host passes several unrelated workspace paths and none matches, the root is
`None` and confinement is skipped. **Fix:** pass `--workspace <dir>` explicitly,
or run from inside the repo so the root resolves.

## Permission prompts you didn't expect

### "secret-bearing path … requires an explicit permission step"

**Not a bug.** Reading a path that looks secret-bearing (`.env`, `.aws`, `.ssh`,
`*.pem`, `*.key`, `id_rsa`, `credentials`, `secrets` …) always force-asks and is
excluded from automatic capture — even under permissive steering. **Fix:**
confirm the prompt if you really intend it; there's no way to silence this by
config, by design.

### "path resolves outside the active workspace"

**Cause:** the read resolves (after following symlinks and `..`) to somewhere
outside the workspace root. **Fix:** confirm the prompt, pass `--workspace` to
widen the root, or set `[workspace] allow_outside_root = true` if this is
routine for your setup.

### "unknown output bound for '<prog>'"

**Cause:** under `guarded` mode, a command the classifier doesn't recognize is
force-asked in case its output is large. **Fix:** if it's safe and bounded, run
it directly and confirm; if it can flood, run it through `ctx run -- …`. To
change the default disposition, set `[guard] unknown_command` (`allow` / `deny`
/ `ask` / `force_ask`).

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
No. Capture is entirely local — an on-disk SQLite + blob store outside the repo.
The optional observer proxy relays your existing API traffic byte-exact on
loopback and records only usage/window metadata, never request bodies or auth
headers.

**Will it ever delete or rewrite my transcript history?**
No. Omitted content is *elided behind an address*, never deleted, and history is
never edited. Anything left out of a digest keeps a coordinate you can retrieve.

**Does it change task outcomes?**
In measured A/Bs, task success is at parity; the wins are cost, latency, turns,
and evidence preservation. When output is small, digests pass it through roughly
1:1. See [Why Straitjacket](WHY-STRAITJACKET.md) and [`evals/`](../evals/).

**Do I need ripgrep / ctags / other binaries?**
No. They accelerate or enrich analysis, but every path has a pure-Python
fallback with the same output contract. `ctx doctor` shows which engine is
active.

**How do I set up just one host?**
`ctx wrap antigravity`, `ctx wrap claude`, or `ctx wrap codex` each set up
exactly one. `ctx wrap setup` does all three.

**How do I preview what setup will write without touching anything?**
`ctx wrap <host> --print-config`.

**How do I run a one-off session that leaves nothing behind?**
`ctx wrap claude -- -p "…"` injects host settings for that process only and
removes them on exit.

**How do I turn the harness off?**
Set `[guard] mode = "advisory"` to make the guard a no-op, or remove the host
integration. For a single break-glass command, confirm the force-ask prompt.

**How do I reclaim disk?**
`ctx gc` mark-and-sweeps expired artifacts (retention is `[store] retention_days`,
default 30); `ctx pin` protects an artifact from collection.

**Where do I change budgets, scopes, or redaction?**
All in `ctx.toml` — see the [Configuration reference](CONFIGURATION.md).

---

[Getting started](GETTING-STARTED.md) · [Configuration](CONFIGURATION.md) · [CLI guide](CLI.md) · [Concepts](CONCEPTS.md)
