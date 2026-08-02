"""Plugin rendering, installation, and health checks (SPEC §4, §18).

``ctx antigravity install`` renders the plugin template into
``<workspace>/.agents/plugins/ctx-harness/`` with the absolute ``ctx``
executable path baked into hooks and MCP config, so the hook never depends
on the invoking process's CWD or PATH.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ctx import __version__
from ctx.config import CONFIG_FILENAME, IGNORE_FILENAME
from ctx.workspace import Workspace

PLUGIN_DIRNAME = "ctx-harness"

_CTX_TOML_TEMPLATE = """\
version = 1
# Optional stable project key for cross-clone artifact continuity.
# repo_key = "my-project"

[workspace]
allow_outside_root = false
follow_symlinks = false
nested_repos = "separate"
respect_gitignore = true

[budgets]
digest_tokens = 480
result_tokens = 1200
turn_retrieval_tokens = 2800
max_inline_bytes = 16384
max_inline_lines = 240
max_matches = 80

[guard]
mode = "guarded"               # advisory | guarded | strict
unknown_command = "force_ask"  # allow | deny | ask | force_ask
internal_error = "allow"       # availability-safe default for context guard

[store]
backend = "user-state"
retention_days = 30

[redaction]
enabled = true
patterns = ["aws-access-key", "private-key", "generic-api-token"]
"""

_CTXIGNORE_TEMPLATE = """\
# Additional capture exclusions; .gitignore is also respected by default.
.env
.env.*
**/.env
**/.env.*
**/secrets/**
**/credentials/**
**/*.pem
**/*.key
**/id_rsa*
**/node_modules/**
**/.venv/**
**/dist/**
**/build/**
"""


def _template_dir(name: str = "antigravity", *, sentinel: str = "plugin.json") -> Path:
    """Locate a packaged host template: repo checkout first, then a data dir
    next to the installed package. ``name`` selects the host (antigravity,
    codex); ``sentinel`` is a file that must exist for the dir to be valid."""
    here = Path(__file__).resolve()
    for candidate in (
        here.parent.parent.parent / "plugins" / name,  # src checkout
        here.parent / "data" / name,  # wheel data (future)
    ):
        if (candidate / sentinel).is_file():
            return candidate
    raise FileNotFoundError(
        f"{name} template not found; reinstall ctx-harness"
    )


def _ctx_executable() -> str:
    exe = shutil.which("ctx")
    if exe:
        return exe
    # Fall back to `python -m ctx` semantics via the current interpreter.
    return f"{sys.executable} -m ctx"


def render_plugin(workspace_root: Path, ctx_exe: str | None = None) -> Path:
    """Render the plugin into ``<workspace>/.agents/plugins/ctx-harness/``."""
    template = _template_dir()
    exe = ctx_exe or _ctx_executable()

    skills_standalone = workspace_root / ".agents" / "skills" / PLUGIN_DIRNAME
    if skills_standalone.exists():
        raise RuntimeError(
            f"standalone skill already installed at {skills_standalone}; "
            "remove it first — the plugin contains the skill (SPEC §4.3)"
        )

    dest = workspace_root / ".agents" / "plugins" / PLUGIN_DIRNAME
    dest.mkdir(parents=True, exist_ok=True)

    for name in ("plugin.json", "hooks.json", "mcp_config.json"):
        content = (template / name).read_text(encoding="utf-8")
        content = content.replace("{{CTX_EXECUTABLE}}", exe)
        json.loads(content)  # rendered manifests must stay valid JSON
        (dest / name).write_text(content, encoding="utf-8")

    for subdir in ("skills", "agents"):
        src = template / subdir
        dst = dest / subdir
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    return dest


def antigravity_settings_path() -> Path:
    """Global Antigravity CLI settings (where the status line is configured).
    Honours GEMINI_CLI_CONFIG_DIR / XDG-style overrides isn't documented, so
    use the published default: ~/.gemini/antigravity-cli/settings.json."""
    import os

    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".gemini" / "antigravity-cli" / "settings.json"


def install_antigravity_statusline(exe: str, *, settings_path: Path | None = None) -> str:
    """Add a ctx status line to the global Antigravity CLI settings, merging
    non-destructively (never clobber a user's existing statusLine/title).
    Antigravity has no dollar-cost field, so the line prices tokens through
    ctx.pricing. Returns a one-line status. Fail-open."""
    path = settings_path or antigravity_settings_path()
    try:
        try:
            existing = _read_settings_object(path)
        except SettingsUnreadable as e:
            # This file is global: clobbering it costs the user every project,
            # not just this one. A status line is never worth that.
            return _refusal(path, e, what="the Antigravity status line")
        if existing.get("statusLine"):
            return f"statusLine already set in {path}; left unchanged"
        merged = dict(existing)
        merged["statusLine"] = {
            "command": f"{exe} statusline antigravity",
            "enabled": True,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        return f"added statusLine to {path}"
    except Exception as e:  # never let statusline setup break wrap
        return f"statusLine not installed ({type(e).__name__}); set it manually"


def install_antigravity(ws: Workspace, *, init_policy: bool = True) -> str:
    dest = render_plugin(ws.root)
    lines = [f"installed plugin: {dest}"]
    exe = _ctx_executable()
    lines.append(install_antigravity_statusline(exe))
    if init_policy:
        lines.extend(init_workspace(ws.root, quiet=True))
    lines.append("")
    lines.append("recommended Antigravity permission settings (user-controlled, not modified):")
    lines.append("  Allow: ctx search / ctx get / ctx stats (bounded retrieval)")
    lines.append("  Ask:   ctx run (command execution keeps the native permission flow)")
    lines.append("")
    lines.append("validate with: ctx doctor --antigravity")
    return "\n".join(lines)


def init_workspace(root: Path, *, quiet: bool = False) -> list[str]:
    lines: list[str] = []
    cfg = root / CONFIG_FILENAME
    ign = root / IGNORE_FILENAME
    if not cfg.exists():
        cfg.write_text(_CTX_TOML_TEMPLATE, encoding="utf-8")
        lines.append(f"wrote {cfg.name}")
    elif not quiet:
        lines.append(f"{cfg.name} already exists; left unchanged")
    if not ign.exists():
        ign.write_text(_CTXIGNORE_TEMPLATE, encoding="utf-8")
        lines.append(f"wrote {ign.name}")
    elif not quiet:
        lines.append(f"{ign.name} already exists; left unchanged")
    return lines


def claude_hook_settings(ctx_exe: str) -> dict:
    """Claude Code / Codex-compatible hook settings dict (PreToolUse guard +
    PostToolUse emission gate). Single source of truth; `ctx.wrap` reuses it
    for ephemeral runs and `install_claude` for the persistent install."""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{ctx_exe} hook claude-code pre-tool-use",
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{ctx_exe} hook claude-code post-tool-use",
                            "timeout": 10,
                        }
                    ],
                }
            ],
            # Input-side pre-flight: 'bound before bloat'. Audits the capability
            # surface once at session start and injects a bounded advisory if it
            # exceeds the [surface] budget in ctx.toml.
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{ctx_exe} hook claude-code session-start",
                            "timeout": 15,
                        }
                    ],
                }
            ],
        }
    }


_GATEWAY_NAME = "ctx-surface-gateway"


def install_gateway(ws: "Workspace", host: str, *, apply: bool = False) -> str:
    """Wire a host to load ONLY the progressive-disclosure gateway instead of
    every MCP server directly, so unrevealed tool schemas never enter context
    ('bound before bloat'). Snapshots the current backends to
    ``.ctx-surface/backends.json`` (the gateway reads them from there) and
    emits a gateway-only launch config per host under ``.ctx-surface/``. Never
    rewrites the user's existing configs — the gateway config is a separate
    file the host loads exclusively. Fail-open."""
    from ctx import surface
    from ctx.surface_gateway import _is_gateway_argv

    root = ws.root
    exe = _ctx_executable()
    lines: list[str] = []

    backends = {n: {"command": a[0], "args": a[1:]}
                for n, a in surface._mcp_server_commands(root).items()
                if not _is_gateway_argv(a)}
    gw_args = ["surface", "gateway", "--workspace", str(root)]

    files: dict[str, str] = {
        ".ctx-surface/backends.json": json.dumps({"mcpServers": backends}, indent=2) + "\n",
    }
    if host == "claude":
        files[".ctx-surface/gateway.claude.json"] = json.dumps(
            {"mcpServers": {_GATEWAY_NAME: {"command": exe, "args": gw_args}}}, indent=2) + "\n"
        launch = f"claude --strict-mcp-config --mcp-config .ctx-surface/gateway.claude.json"
    elif host == "codex":
        args_toml = ", ".join(json.dumps(a) for a in gw_args)
        files[".ctx-surface/config.codex.gateway.toml"] = (
            "# ctx surface gateway — load ONLY the gateway; it fronts the\n"
            "# backends recorded in .ctx-surface/backends.json.\n"
            f"[mcp_servers.{_GATEWAY_NAME}]\n"
            f'command = "{exe}"\nargs = [{args_toml}]\n')
        launch = f"codex --config .ctx-surface/config.codex.gateway.toml"
    elif host == "antigravity":
        files[".ctx-surface/mcp_config.gateway.json"] = json.dumps(
            {"mcpServers": {_GATEWAY_NAME: {"command": exe, "args": gw_args, "disabled": False}}},
            indent=2) + "\n"
        launch = ("point ~/.gemini/antigravity-cli at "
                  f".ctx-surface/mcp_config.gateway.json, then Refresh MCP servers")
    else:
        return f"unknown host {host!r}; one of claude, codex, antigravity"

    lines.append(f"gateway wiring for {host}: fronts {len(backends)} backend(s) "
                 f"[{', '.join(sorted(backends)) or 'none'}]")
    if apply:
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        lines.append("  wrote: " + ", ".join(files))
        lines.append("  tip: set [surface] gateway = true in ctx.toml so the "
                     "SessionStart gate knows disclosure is progressive")
    else:
        lines.append("  preview — pass --apply to write the gateway config")
    lines.append(f"  launch: {launch}")
    return "\n".join(lines)


def claude_statusline_setting(ctx_exe: str) -> dict:
    """Claude Code ``statusLine`` block: a command that receives session JSON
    on stdin and prints one line (model · context · cost · git). Cost comes
    from the host's own ``cost.total_cost_usd`` when present, else ctx prices
    the tokens. Single source of truth, reused by wrap and install."""
    return {
        "type": "command",
        "command": f"{ctx_exe} statusline claude-code",
        "padding": 0,
    }


class SettingsUnreadable(Exception):
    """A host settings file exists but does not parse.

    Raised instead of defaulting to ``{}``: every caller merges into the parsed
    object and writes the whole thing back, so an empty default is not a
    lenient fallback — it silently deletes whatever the user had configured.
    """


def _read_settings_object(path: Path) -> dict:
    """Parse a settings file that is about to be merged into and rewritten.

    Missing file → ``{}`` (nothing to lose). Present but unparseable → raise,
    because the caller's next move is a full overwrite.
    """
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SettingsUnreadable(
            f"{path.name} is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})"
        ) from e
    if not isinstance(parsed, dict):
        raise SettingsUnreadable(
            f"{path.name} contains a JSON {type(parsed).__name__}, not an object"
        )
    return parsed


def _refusal(path: Path, err: Exception, *, what: str) -> str:
    """What we say instead of destroying someone's configuration."""
    return "\n".join(
        [
            f"cannot set up {what}: {err}",
            f"ctx did not modify {path} — merging into it means writing the whole",
            "file back, which would have discarded everything already in it.",
            "fix the JSON (or move the file aside) and re-run: ctx wrap setup",
        ]
    )


def _iter_hook_commands(settings: object):
    """Yield every hook ``command`` string in an agent settings document,
    tolerating any shape.

    These files are foreign input — hand-edited, written by another tool, or
    truncated mid-write — and the install path documents a graceful refusal
    for malformed ones. The straightforward traversal
    (``settings["hooks"].values()`` → iterate → ``group["hooks"]``) makes three
    unchecked shape assumptions, and a bug bash confirmed that any of them
    raises instead of refusing: ``hooks`` as a list gave an AttributeError out
    of ``ctx wrap setup``, not the documented message.

    Being shape-tolerant here, once, is the mechanism. Callers get "no ctx
    hook found" for a malformed document and go on to the normal refusal path,
    rather than each one growing its own try/except.
    """
    if not isinstance(settings, dict):
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for stage in hooks.values():
        if not isinstance(stage, list):
            continue
        for group in stage:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                continue
            for hook in entries:
                if isinstance(hook, dict):
                    yield str(hook.get("command", ""))


def _hook_command_present(settings: dict, ctx_exe: str) -> bool:
    """True if a ctx-harness hook command is already registered."""
    for cmd in _iter_hook_commands(settings):
        if " hook claude-code " in cmd or cmd.endswith("hook claude-code"):
            return True
    return False


def install_claude(ws: Workspace, *, init_policy: bool = True) -> str:
    """Persistent Claude Code install: merge harness hooks into project
    ``.claude/settings.json`` and drop the explorer agent. Idempotent and
    non-destructive — existing hooks and settings are preserved."""
    root = ws.root
    exe = _ctx_executable()
    lines: list[str] = []

    settings_path = root / ".claude" / "settings.json"
    try:
        existing = _read_settings_object(settings_path)
    except SettingsUnreadable as e:
        return _refusal(settings_path, e, what="Claude Code")
    merged = dict(existing)
    changed = False
    if _hook_command_present(existing, exe):
        lines.append(".claude/settings.json hooks already harnessed; left unchanged")
    else:
        bucket = merged.setdefault("hooks", {})
        if not isinstance(bucket, dict):
            # Refuse the same way an unparseable file is refused, a few lines
            # above: a message and an untouched file. Raising here would have
            # swapped one unhandled exception for another.
            # Fourth door onto one guard. _iter_hook_commands was hardened so
            # READING a malformed settings.json refuses gracefully, and
            # wrap._wrap_claude_merged got the same treatment for the
            # ephemeral path -- but this MERGE still called .setdefault on
            # whatever was there, so a `hooks` list crashed install_claude
            # with a raw AttributeError instead of the documented refusal.
            return _refusal(
                settings_path,
                SettingsUnreadable(
                    f"its 'hooks' value is a JSON {type(bucket).__name__}, "
                    "not an object"
                ),
                what="Claude Code",
            )
        for stage, entries in claude_hook_settings(exe)["hooks"].items():
            bucket.setdefault(stage, []).extend(entries)
        lines.append("merged PreToolUse/PostToolUse hooks")
        changed = True
    # Status line, independently idempotent: add only when the user has none,
    # so pre-statusline installs gain it on re-run but a custom line is never
    # clobbered.
    if not existing.get("statusLine"):
        merged["statusLine"] = claude_statusline_setting(exe)
        lines.append("added statusLine (model · context · cost · git)")
        changed = True
    else:
        lines.append("statusLine already set; left unchanged")
    if changed:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        lines.append(f"wrote {settings_path.relative_to(root)}")

    agent_src = _template_dir() / "agents" / "ctx-explorer.md"
    agent_dst = root / ".claude" / "agents" / "ctx-explorer.md"
    if agent_dst.exists():
        lines.append(".claude/agents/ctx-explorer.md exists; left unchanged")
    elif agent_src.is_file():
        agent_dst.parent.mkdir(parents=True, exist_ok=True)
        agent_dst.write_bytes(agent_src.read_bytes())
        lines.append(f"wrote {agent_dst.relative_to(root)}")

    # The teaching surface Claude Code otherwise lacks (measured in
    # evals/ask-diagnose-3arm-2026-07-20.md: agents adopt ctx ask/q only
    # when the vocabulary is in context). Marker-delimited and versioned,
    # mirroring the Codex AGENTS.md block — refreshed on re-run, cleanly
    # removable. Claude Code reads CLAUDE.md into the session automatically.
    lines.append(_upsert_agents_block(root / "CLAUDE.md", _render_claude_md(exe)))

    if init_policy:
        lines.extend(init_workspace(root, quiet=True))
    lines.append(
        "uninstall: remove the ctx hook entries from .claude/settings.json "
        "and the ctx-harness block from CLAUDE.md"
    )
    return "\n".join(lines)


_CODEX_MARK_START = "<!-- ctx-harness:start -->"
_CODEX_MARK_END = "<!-- ctx-harness:end -->"


def _render_codex_file(name: str, exe: str) -> str:
    tmpl = _template_dir("codex", sentinel="config.toml")
    return (tmpl / name).read_text(encoding="utf-8").replace("{{CTX_EXECUTABLE}}", exe)


def _render_claude_md(exe: str) -> str:
    """The compact ctx verb card for a Claude Code session's CLAUDE.md —
    the vocabulary the wrap/settings path does not otherwise surface
    (evals/ask-diagnose-3arm-2026-07-20.md). Marker-delimited so it is
    idempotent and removable, exactly like the Codex AGENTS.md block."""
    return f"""\
{_CODEX_MARK_START}
## Context harness (ctx / straitjacket)

This workspace is harnessed by `{exe}`. Noisy command and file output is
captured at the source and returned as a bounded, addressable digest — work
*with* it rather than paging or pre-filtering around it.

- Answer repository questions with `ctx ask "<q>" --intent
  locate|impact|diagnose` — one bounded evidence view instead of a
  search/read/search loop. `diagnose` reads the last captured run's failure
  facts and names the culprit symbol; it never reruns tests. `locate` =
  where is X defined/used; `impact` = what breaks if X changes.
- Compose typed facts with `ctx q '<stage> | <stage>'` (bounded, total)
  instead of piping through grep/awk/jq: `refs X | group file | top 3 | get`,
  `fails last | in-changed` (failing tests in changed symbols),
  `corpus --ext py --changed | outline`, `records run:<id>#stdout --jsonl |
  group level | count`.
- Capture noisy commands with `ctx run -- <cmd>`; pull exact bytes with
  `ctx get <handle>`. Cite handles/coordinates instead of re-quoting output.
{_CODEX_MARK_END}
"""


def _upsert_agents_block(path: Path, block: str) -> str:
    """Insert or replace the ctx-harness block in AGENTS.md, preserving the
    rest of the file. Returns a one-line status."""
    block = block.strip() + "\n"
    if not path.is_file():
        path.write_text(block, encoding="utf-8")
        return f"wrote {path.name}"
    text = path.read_text(encoding="utf-8")
    if _CODEX_MARK_START in text and _CODEX_MARK_END in text:
        pre = text[: text.index(_CODEX_MARK_START)]
        post = text[text.index(_CODEX_MARK_END) + len(_CODEX_MARK_END) :]
        path.write_text(pre + block + post.lstrip("\n"), encoding="utf-8")
        return f"refreshed ctx-harness block in {path.name}"
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.write_text(text + sep + block, encoding="utf-8")
    return f"appended ctx-harness block to {path.name}"


def install_codex(ws: Workspace, *, init_policy: bool = True) -> str:
    """Persistent Codex CLI install: register the ctx MCP server + lifecycle
    hooks under ``.codex/`` and add a harness section to AGENTS.md. Never
    clobbers an existing ``.codex/config.toml`` — prints the snippet to add."""
    root = ws.root
    exe = _ctx_executable()
    lines: list[str] = []
    codex_dir = root / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    cfg = codex_dir / "config.toml"
    cfg_snippet = _render_codex_file("config.toml", exe)
    if not cfg.is_file():
        cfg.write_text(cfg_snippet, encoding="utf-8")
        lines.append("wrote .codex/config.toml")
    elif "mcp_servers.ctx-harness" in cfg.read_text(encoding="utf-8"):
        lines.append(".codex/config.toml already registers ctx-harness; unchanged")
    else:
        # Never rewrite a user's TOML in place (duplicate-table hazard).
        lines.append(
            ".codex/config.toml exists — add these lines to enable ctx-harness:\n"
            + "\n".join("    " + ln for ln in cfg_snippet.strip().splitlines())
        )

    hooks_path = codex_dir / "hooks.json"
    hooks_rendered = _render_codex_file("hooks.json", exe)
    if not hooks_path.is_file():
        hooks_path.write_text(hooks_rendered, encoding="utf-8")
        lines.append("wrote .codex/hooks.json")
    else:
        try:
            cur = _read_settings_object(hooks_path)
        except SettingsUnreadable as e:
            # Not an early return: config.toml above may already have been
            # written, and claiming otherwise would be its own small lie.
            cur = None
            lines.append(_refusal(hooks_path, e, what="Codex hooks"))
        if cur is None:
            pass
        elif _hook_command_present_codex(cur, exe):
            lines.append(".codex/hooks.json already harnessed; left unchanged")
        else:
            ours = json.loads(hooks_rendered)
            cur.setdefault("hooks", {})
            for stage, entries in ours["hooks"].items():
                cur["hooks"].setdefault(stage, []).extend(entries)
            hooks_path.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
            lines.append("merged ctx-harness hooks into .codex/hooks.json")

    lines.append(_upsert_agents_block(root / "AGENTS.md", _render_codex_file("AGENTS.md", exe)))

    if init_policy:
        lines.extend(init_workspace(root, quiet=True))
    lines.append("uninstall: remove .codex/config.toml, .codex/hooks.json, and the AGENTS.md block")
    return "\n".join(lines)


def _hook_command_present_codex(settings: dict, ctx_exe: str) -> bool:
    # Same shape-tolerant traversal as the Claude reader: .codex/hooks.json is
    # foreign input too, and carried the identical unchecked assumptions.
    for cmd in _iter_hook_commands(settings):
        if " hook codex " in cmd:
            return True
    return False


# The harness is built for Antigravity and works with Claude Code and Codex.
# Both of these are DERIVED from the host registry (ctx.hosts): the wired set
# and the name→installer mapping used to be hand-maintained here as well, a
# second copy of what every HostSpec already declares via `installer`.
def _setup_hosts_tuple() -> tuple[str, ...]:
    """The hosts `ctx wrap setup` configures by default.

    Self-hosted hosts are excluded on purpose. Setup harnesses the agents you
    already have by writing config into them; building a virtualenv and pulling
    a vendor SDK off the network is a different kind of act, and it should be an
    explicit `ctx wrap antigravity-sdk`, never a side effect of `ctx wrap setup`.
    """
    from ctx.hosts import harnessable_hosts

    return tuple(s.name for s in harnessable_hosts() if not s.self_hosted)


SETUP_HOSTS = _setup_hosts_tuple()


def setup_hosts(ws: Workspace, hosts: "list[str] | None" = None) -> str:
    """Single-command multi-host setup. Configures the harness for each named
    host (default: every host the registry declares an installer for) and
    returns a combined per-host report."""
    from ctx.hosts import host_by_name, installer_for

    selected = hosts or list(SETUP_HOSTS)
    out: list[str] = [
        "ctx harness setup — built for Antigravity, works with Claude Code and Codex",
        "",
    ]
    for host in selected:
        spec = host_by_name(host)
        installer = installer_for(spec) if spec else None
        out.append(f"── {host} ──")
        if installer is None:
            out.append(f"unknown host {host!r} (choose from {', '.join(SETUP_HOSTS)})")
        else:
            # Each host inits policy once; keep it quiet after the first.
            out.append(installer(ws, init_policy=(host == selected[0])))
        out.append("")
    out.append("validate: ctx doctor --antigravity   (and inspect .claude/ / .codex/)")
    return "\n".join(out).rstrip() + "\n"


def doctor_checks(ws: Workspace, *, antigravity: bool = False) -> list[tuple[str, bool, str]]:
    """The health checks themselves, as (name, ok, detail) rows.

    Split out from :func:`doctor_report` so the guided setup can *verify with
    the same checks the doctor runs* rather than keeping a second opinion about
    what "healthy" means — the duplicate-source-of-truth pattern this codebase
    has been bitten by more than once."""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    exe = shutil.which("ctx")
    check("ctx on PATH", exe is not None, exe or "not found (hook uses absolute path)")

    check("workspace resolved", True, f"id={ws.workspace_id} root=<workspace>")
    check(
        "policy loaded",
        True,
        f"guard={ws.config.guard.mode} digest_tokens={ws.config.budgets.digest_tokens}",
    )

    try:
        from ctx.store import Store

        store = Store(ws.workspace_id)
        probe = store.put_blob(b"ctx-doctor-probe")
        store.get_blob(probe)
        check("store writable", True, "user-state backend")
    except Exception as e:
        check("store writable", False, str(e))

    plugin_dir = ws.root / ".agents" / "plugins" / PLUGIN_DIRNAME
    skill_dir = ws.root / ".agents" / "skills" / PLUGIN_DIRNAME
    if antigravity:
        check("plugin installed", plugin_dir.is_dir(), str(plugin_dir.relative_to(ws.root)) if plugin_dir.is_dir() else "run: ctx antigravity install")
        if plugin_dir.is_dir():
            for name in ("plugin.json", "hooks.json", "mcp_config.json"):
                p = plugin_dir / name
                ok = False
                detail = "missing"
                if p.is_file():
                    try:
                        json.loads(p.read_text(encoding="utf-8"))
                        ok = True
                        detail = "valid JSON"
                    except json.JSONDecodeError as e:
                        detail = f"invalid JSON: {e}"
                check(f"plugin {name}", ok, detail)
            check(
                "plugin embeds skill",
                (plugin_dir / "skills" / PLUGIN_DIRNAME / "SKILL.md").is_file(),
                "",
            )
            agent = plugin_dir / "agents" / "ctx-explorer.md"
            check(
                "explorer agent",
                agent.is_file(),
                "agents/ctx-explorer.md" if agent.is_file() else "missing — re-run ctx antigravity install",
            )
        else:
            check("explorer agent", True, "plugin not installed")
        dup = plugin_dir.is_dir() and skill_dir.is_dir()
        check(
            "no duplicate installation",
            not dup,
            "plugin and standalone skill are both installed — remove one (SPEC §4.3)" if dup else "",
        )

    # What `ctx wrap setup` actually wrote. Until now doctor never opened
    # these files, so it happily printed OK for a workspace where the hooks
    # had never been installed, or had been clobbered — the one question the
    # command exists to answer.
    wrapped: list[str] = []
    for label, rel, present_fn in (
        ("claude hooks", ".claude/settings.json", _hook_command_present),
        ("codex hooks", ".codex/hooks.json", _hook_command_present_codex),
    ):
        path = ws.root / rel
        if not path.is_file():
            continue
        try:
            data = _read_settings_object(path)
        except SettingsUnreadable as e:
            check(label, False, f"{e} — fix the file, then re-run: ctx wrap setup")
            continue
        hooked = present_fn(data, exe or "")
        check(
            label,
            hooked,
            rel if hooked else f"{rel} has no ctx hook entry — run: ctx wrap setup",
        )
        if hooked:
            wrapped.append(label.split()[0])
    if plugin_dir.is_dir():
        wrapped.append("antigravity")
    check(
        "an agent is wrapped",
        bool(wrapped),
        " + ".join(wrapped) if wrapped else "nothing is hooked yet — run: ctx wrap setup",
    )

    rg = shutil.which("rg")
    check(
        "search engine",
        True,
        f"ripgrep ({rg})" if rg else "python fallback (install ripgrep for large repos)",
    )

    try:
        import pathspec  # noqa: F401

        check("ignore matching", True, "pathspec (gitignore semantics)")
    except ImportError:
        check("ignore matching", True, "fnmatch fallback (pip install pathspec)")

    # Validate a real manifest against the vendored wire schema when the
    # optional validator is installed — catches schema drift early.
    try:
        import jsonschema

        schema_path = Path(__file__).resolve().parent.parent.parent / "spec" / "schemas" / "invocation-v1.schema.json"
        if schema_path.is_file():
            from ctx.store import Store as _S

            _store = _S(ws.workspace_id)
            row = _store.db.execute(
                "SELECT id FROM objects WHERE kind='run' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                manifest = _store.get_manifest(row[0])
                jsonschema.validate(manifest, json.loads(schema_path.read_text(encoding="utf-8")))
                check("manifest schema", True, "latest run manifest validates against invocation-v1")
    except ImportError:
        pass
    except Exception as e:
        check("manifest schema", False, str(e)[:120])

    # Hook self-test: classifier must emit a decision for a known flood.
    try:
        from ctx.hook import classify

        d = classify(
            {
                "tool_name": "run_command",
                "tool_input": {"CommandLine": "pytest -q", "Cwd": str(ws.root)},
                "workspacePaths": [str(ws.root)],
            }
        )
        check("hook classifier", d.get("decision") == "deny", f"pytest → {d.get('decision')}")
    except Exception as e:
        check("hook classifier", False, str(e))

    # Cumulative savings from the telemetry ledger (operational, not identity).
    try:
        from ctx.retrieval import telemetry_summary
        from ctx.store import Store as _Store

        t = telemetry_summary(_Store(ws.workspace_id))
        if t["events"]:
            check(
                "telemetry",
                True,
                f"{t['events']} ops · est {t['est_tokens_avoided']:,} prompt tokens avoided",
            )
    except Exception:
        pass

    return checks


def doctor_report(ws: Workspace, *, antigravity: bool = False) -> str:
    """Render :func:`doctor_checks` as the `ctx doctor` report."""
    checks = doctor_checks(ws, antigravity=antigravity)
    ok_all = all(ok for _, ok, _ in checks)
    lines = [f"[ctx doctor v{__version__}] {'OK' if ok_all else 'PROBLEMS FOUND'}"]
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        lines.append(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return "\n".join(lines)


def install_agy_sdk(ws, ctx_exe: str | None = None, **_kw) -> str:
    """Install the ctx-owned Antigravity SDK environment.

    There is nothing workspace-scoped to write: this host has no hooks, no
    plugin and no config file — its containment is compiled into the agent's
    tools. The whole install is the managed venv plus the launcher, which is
    machine-scoped, so this is idempotent across workspaces.
    """
    from ctx.agysdk import ensure_venv, launcher_path

    ok, msg = ensure_venv()
    return f"{msg}\nlauncher: {launcher_path()}" if ok else f"not installed: {msg}"
