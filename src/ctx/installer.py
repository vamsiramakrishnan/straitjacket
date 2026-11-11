"""Plugin rendering, installation, and health checks (SPEC §4, §18).

``ctx antigravity install`` renders the plugin template into
``<workspace>/.agents/plugins/ctx-harness/`` with the absolute ``ctx``
executable path baked into hooks and MCP config, so the hook never depends
on the invoking process's CWD or PATH.
"""

from __future__ import annotations

import json
import os
import re
import shlex
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
patterns = ["aws-access-key", "aws-secret-key", "private-key", "github-token", "gitlab-token", "slack-token", "stripe-key", "twilio-key", "sendgrid-key", "npm-token", "pypi-token", "huggingface-token", "anthropic-key", "jwt", "google-api-key", "generic-api-token"]  # fix: template pinned only 3 of 16 code-default patterns
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
    """Return a shell-safe command string for hook command fields."""
    exe = shutil.which("ctx")
    if exe:
        return shlex.join([exe])
    # Fall back to `python -m ctx` semantics via the current interpreter.
    return shlex.join([sys.executable, "-m", "ctx"])


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
    lines.append(
        "uninstall: remove .agents/plugins/ctx-harness; remove the global "
        "statusLine only when its command is the ctx status-line command"
    )
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
    for ephemeral runs and `install_claude` for the persistent install.

    The matchers are derived from the governance table (`ctx.governance`),
    not typed here: PreToolUse names exactly the tools with an input-side
    route; PostToolUse fires for every tool except the declared exemptions,
    so a sub-agent, an MCP tool, or a built-in added in a later release is
    gated rather than skipped; PostToolUseFailure observes failed tools (the
    edit-outcome ledger's failure half, and failure output, which no hook
    can replace)."""
    from ctx.governance import claude_post_matcher, claude_pre_matcher

    post_matcher = claude_post_matcher()
    return {
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{
                "type": "command",
                "command": f"{ctx_exe} hook claude-code user-prompt-submit",
                "timeout": 10,
            }]}],
            "PreToolUse": [
                {
                    "matcher": claude_pre_matcher(),
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
                    "matcher": post_matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{ctx_exe} hook claude-code post-tool-use",
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "PostToolUseFailure": [
                {
                    "matcher": post_matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{ctx_exe} hook claude-code post-tool-use-failure",
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
        launch = "claude --strict-mcp-config --mcp-config .ctx-surface/gateway.claude.json"
    elif host == "codex":
        args_toml = ", ".join(json.dumps(a) for a in gw_args)
        files[".ctx-surface/config.codex.gateway.toml"] = (
            "# ctx surface gateway — load ONLY the gateway; it fronts the\n"
            "# backends recorded in .ctx-surface/backends.json.\n"
            f"[mcp_servers.{_GATEWAY_NAME}]\n"
            f'command = "{exe}"\nargs = [{args_toml}]\n')
        launch = "codex --config .ctx-surface/config.codex.gateway.toml"
    elif host == "antigravity":
        files[".ctx-surface/mcp_config.gateway.json"] = json.dumps(
            {"mcpServers": {_GATEWAY_NAME: {"command": exe, "args": gw_args, "disabled": False}}},
            indent=2) + "\n"
        launch = ("point ~/.gemini/antigravity-cli at "
                  ".ctx-surface/mcp_config.gateway.json, then Refresh MCP servers")
    else:
        return f"unknown host {host!r}; one of claude, codex, antigravity"

    lines.append(f"gateway wiring for {host}: fronts {len(backends)} backend(s) "
                 f"[{', '.join(sorted(backends)) or 'none'}]")
    if apply:
        from ctx.surface_profiles import write_private

        for rel, content in files.items():
            write_private(root / rel, content)  # 0600: carries full server commands
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


def merge_hook_stages(doc: dict, stages: dict) -> None:
    """Append our hook entries into a foreign settings document, in place.

    THE MERGE, not a check beside it. ``install_claude`` grew an
    ``isinstance(bucket, dict)`` guard after a bug bash crashed it on a
    ``"hooks": []``; ``install_codex`` performs the identical merge four
    hundred lines away and did not inherit it, so the next bash crashed the
    Codex door with the same raw AttributeError. That is the fifth door onto
    this guard. A check every caller must remember is not a mechanism -- the
    merge itself is now the only way to do this, so a sixth host cannot
    forget it.

    Raises SettingsUnreadable, which every caller already answers with
    ``_refusal``: an untouched file and a message naming the problem. The
    per-stage list check comes along for free, and closes a shape neither
    bash reported (``{"hooks": {"PreToolUse": "nope"}}`` reached ``.extend``
    on a string).
    """
    bucket = doc.setdefault("hooks", {})
    if not isinstance(bucket, dict):
        raise SettingsUnreadable(
            f"its 'hooks' value is a JSON {type(bucket).__name__}, not an object"
        )
    for stage, entries in stages.items():
        current = bucket.setdefault(stage, [])
        if not isinstance(current, list):
            raise SettingsUnreadable(
                f"its 'hooks.{stage}' value is a JSON "
                f"{type(current).__name__}, not an array"
            )
        current.extend(entries)


def _refusal(path: Path, err: Exception, *, what: str) -> str:
    """What we say instead of destroying someone's configuration."""
    return "\n".join(
        [
            f"cannot set up {what}: {err}",
            f"ctx did not modify {path} — merging into it means writing the whole",
            "file back, which would have discarded everything already in it.",
            "fix the JSON (or move the file aside) and re-run: ctx setup",
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
    of ``ctx setup``, not the documented message.

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


_CLAUDE_STAGE_SLUG = {
    "UserPromptSubmit": "user-prompt-submit", "PreToolUse": "pre-tool-use",
    "PostToolUse": "post-tool-use", "PostToolUseFailure": "post-tool-use-failure",
    "SessionStart": "session-start",
}


#: Every default matcher an earlier release shipped for a ctx-owned group,
#: taken from the repository history (src/ctx/wrap.py before the installer
#: owned it, then src/ctx/installer.py). Only a matcher equal to one of these
#: is upgraded; any other value is the user's and is reported, not replaced.
_PREVIOUS_CLAUDE_DEFAULTS: dict[str, frozenset[str]] = {
    "PreToolUse": frozenset({
        "Bash|Read",
        "Bash|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit",
    }),
    "PostToolUse": frozenset({
        "Bash|Read|Edit|Write",
        "Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*",
    }),
}


def _is_ctx_hook_command(command: object, slug: str) -> bool:
    """Is this exactly the invocation the installer writes, ``<exe> hook
    claude-code <slug>``, under any executable path?

    Parsed, not suffix-matched: a user's ``echo done; /x/ctx hook claude-code
    post-tool-use`` ends with the same text and is theirs. Allowed forms are
    ``<path>/ctx`` or ``<python> -m ctx`` followed by exactly the three
    arguments; any shell operator, redirection or extra word disqualifies.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if any(t and all(c in "();<>|&" for c in t) for t in tokens):
        return False
    if len(tokens) < 4 or tokens[-3:] != ["hook", "claude-code", slug]:
        return False
    prefix = tokens[:-3]
    if len(prefix) == 1:
        # `ctx-hook-native` is the Rust post-tool-use shim earlier releases
        # wrote in place of ctx (wrap.py `_post_hook_exe`, removed since).
        return os.path.basename(prefix[0]) in ("ctx", "ctx.exe", "ctx-hook-native")
    if len(prefix) == 3 and prefix[1:] == ["-m", "ctx"]:
        return os.path.basename(prefix[0]).startswith("python")
    return False


def _owned_claude_group(group: object, stage: str) -> bool:
    """A matcher group ctx owns: every hook in it is exactly a ctx command
    for ``stage`` (see :func:`_is_ctx_hook_command`)."""
    if not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    slug = _CLAUDE_STAGE_SLUG.get(stage)
    if not slug or not isinstance(entries, list) or not entries:
        return False
    return all(isinstance(h, dict) and _is_ctx_hook_command(h.get("command"), slug)
               for h in entries)


def _stage_has_ctx_hook(groups: object, stage: str) -> bool:
    """Does any group of this stage hold an exact ctx hook for it?

    KNOWN LIMITATION (documented): a ctx hook written in a command form the
    installer does not recognize (anything but ``<…/ctx>``, ``<…/ctx-hook-
    native>`` or ``<python> -m ctx`` followed by ``hook claude-code <stage>``,
    e.g. a hand-written ``uvx ctx-harness …``) is not counted, so setup adds
    a second ctx group beside it and both run. Recognizing arbitrary
    launchers would let any user command that ends the same way be treated
    as ctx's, which is the worse failure."""
    slug = _CLAUDE_STAGE_SLUG.get(stage)
    if not slug or not isinstance(groups, list):
        return False
    return any(
        isinstance(g, dict) and isinstance(g.get("hooks"), list)
        and any(isinstance(h, dict) and _is_ctx_hook_command(h.get("command"), slug)
                for h in g["hooks"])
        for g in groups)


def update_owned_claude_matchers(existing_hooks: object, stages: dict) -> list[str]:
    """Bring ctx-owned matchers up to date, in place; one line per finding.

    Upgraded: a ctx-owned group whose matcher equals a default an earlier
    release shipped. Reported and left alone: a ctx-owned group with any
    other matcher (customized), and a ctx hook sharing a group with a user
    hook (that group's matcher is not ctx's to change). Foreign groups and
    malformed shapes are never touched.
    """
    notes: list[str] = []
    if not isinstance(existing_hooks, dict):
        return notes
    for stage, entries in stages.items():
        groups = existing_hooks.get(stage)
        slug = _CLAUDE_STAGE_SLUG.get(stage)
        if not isinstance(groups, list) or not entries or not slug:
            continue
        want = entries[0].get("matcher")
        if want is None:
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            current = group.get("matcher")
            if _owned_claude_group(group, stage):
                if current == want:
                    continue
                if current in _PREVIOUS_CLAUDE_DEFAULTS.get(stage, frozenset()):
                    group["matcher"] = want
                    notes.append(f"updated ctx {stage} matcher from a previous default (was {current!r})")
                else:
                    notes.append(
                        f"ctx {stage} matcher is customized ({current!r}); left as is. "
                        f"Current default: {want!r}. To adopt it, set that matcher on the "
                        f"ctx {stage} entry in .claude/settings.json")
            elif isinstance(group.get("hooks"), list) and any(
                    isinstance(h, dict) and _is_ctx_hook_command(h.get("command"), slug)
                    for h in group["hooks"]) and current != want:
                notes.append(
                    f"ctx {stage} hook shares a group with another hook (matcher {current!r}), "
                    f"so ctx did not change it. Current default: {want!r}. To adopt it, move "
                    f"the ctx hook into its own group with that matcher")
        holding = [g for g in groups if isinstance(g, dict) and isinstance(g.get("hooks"), list)
                   and any(isinstance(h, dict) and _is_ctx_hook_command(h.get("command"), slug)
                           for h in g["hooks"])]
        if len(holding) > 1:
            notes.append(
                f"ctx {stage} hook is registered {len(holding)} times (matchers: "
                + ", ".join(repr(g.get("matcher")) for g in holding)
                + "); every copy runs. Keep one: delete the extra ctx "
                f"`hook claude-code {slug}` entries from .claude/settings.json")
    return notes


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
    full_stages = claude_hook_settings(exe)["hooks"]
    # fix: check presence per stage, not any-stage-present, so a reinstall adds a stage an older install lacked
    existing_hooks = existing.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}
    missing_stages = {
        stage: entries
        for stage, entries in full_stages.items()
        if not _stage_has_ctx_hook(existing_hooks.get(stage, []), stage)
    }
    # Entries ctx already owns are brought up to date in place: an older
    # install's matchers (e.g. a PostToolUse that named `Task` and missed
    # `Agent`) would otherwise survive every re-run, because the stage is
    # "present". Ownership is the uninstall convention: a group whose hooks
    # are all ctx `hook claude-code <stage>` commands. Anything else --
    # including a group mixing ctx and foreign hooks -- is left untouched.
    updated = update_owned_claude_matchers(existing_hooks, full_stages)
    for note in updated:
        lines.append(note)
    if any(n.startswith("updated ") for n in updated):
        changed = True
    if not missing_stages:
        if not any(n.startswith("updated ") for n in updated):
            lines.append(".claude/settings.json hooks already harnessed; left unchanged")
    else:
        try:
            merge_hook_stages(merged, missing_stages)
        except SettingsUnreadable as e:
            # Refuse the same way an unparseable file is refused, a few lines
            # above: a message and an untouched file. Raising here would have
            # swapped one unhandled exception for another.
            return _refusal(settings_path, e, what="Claude Code")
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
        "and the ctx-harness block from CLAUDE.md; remove the statusLine only "
        "when its command is ctx, and remove ctx-explorer.md only if setup "
        "reported that it created the file"
    )
    return "\n".join(lines)


_CODEX_MARK_START = "<!-- ctx-harness:start -->"
_CODEX_MARK_END = "<!-- ctx-harness:end -->"


def _render_codex_file(name: str, exe: str) -> str:
    tmpl = _template_dir("codex", sentinel="config.toml")
    rendered = (tmpl / name).read_text(encoding="utf-8")
    if name == "config.toml":
        # Codex launches MCP stdio servers without a shell: `command` is one
        # executable and every remaining token belongs in `args`.  Keeping a
        # fallback such as `/path/python -m ctx` in `command` makes Codex look
        # for a file with spaces in its name and fail with ENOENT.
        argv = shlex.split(exe)
        if not argv:
            raise ValueError("ctx executable command is empty")
        rendered = rendered.replace("{{CTX_MCP_COMMAND}}", json.dumps(argv[0]))
        rendered = rendered.replace(
            "{{CTX_MCP_ARGS}}", json.dumps([*argv[1:], "mcp", "--bounded-only"])
        )
        return rendered
    if name == "hooks.json":
        # Executable paths may contain quotes/backslashes. Substitute decoded
        # string values, then JSON-encode, instead of interpolating raw JSON.
        def substitute(value):
            if isinstance(value, str):
                return value.replace("{{CTX_EXECUTABLE}}", exe)
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            return value
        return json.dumps(substitute(json.loads(rendered)), indent=2) + "\n"
    return rendered.replace("{{CTX_EXECUTABLE}}", exe)


def _codex_mcp_contract(path: Path, exe: str) -> tuple[bool, str]:
    """Validate the exact stdio launch contract Codex must be able to start.

    In particular, ``command`` is one executable and ``args`` carries
    ``-m ctx mcp --bounded-only`` when the Python fallback is in use.  Treating
    the whole shell command as ``command`` is the ENOENT failure this check is
    designed to catch before setup can mint a ready receipt.
    """
    import tomllib

    if path.is_symlink() or path.parent.is_symlink():
        return False, ".codex/config.toml uses a symlink; setup refuses it"
    if not path.is_file():
        return False, ".codex/config.toml is missing"
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, f".codex/config.toml is unreadable: {exc}"
    servers = doc.get("mcp_servers")
    actual = servers.get("ctx-harness") if isinstance(servers, dict) else None
    if not isinstance(actual, dict):
        return False, ".codex/config.toml has no mcp_servers.ctx-harness table"
    if "url" in actual:
        return False, "ctx-harness MCP selects a remote URL instead of the bounded stdio server"

    expected_doc = tomllib.loads(_render_codex_file("config.toml", exe))
    expected = expected_doc["mcp_servers"]["ctx-harness"]
    if actual.get("command") != expected["command"]:
        return False, "ctx-harness MCP command is stale or contains arguments"
    if actual.get("args") != expected["args"]:
        return False, "ctx-harness MCP args are stale or incomplete"
    if actual.get("enabled") is False:
        return False, "ctx-harness MCP is explicitly disabled"
    features = doc.get("features")
    if not isinstance(features, dict) or features.get("hooks") is not True:
        return False, ".codex/config.toml must enable hooks = true in [features]"
    return True, ".codex/config.toml launches the bounded ctx MCP server"


def setup_conflicts(ws: Workspace, hosts: "list[str]") -> list[str]:
    """Return user-owned setup state that cannot be rewritten safely."""
    from ctx.mcp_hosts import HOSTS, conflicts

    problems = [problem for host in hosts if host in HOSTS for problem in conflicts(host, ws.root)]
    return problems + _codex_setup_conflicts(ws, hosts)


def _codex_setup_conflicts(ws: Workspace, hosts: "list[str]") -> list[str]:
    if "codex" not in hosts:
        return []
    from ctx.host_config import plan_configuration

    plan = plan_configuration(ws.root, ["codex"])
    return [plan.render()] if plan.blockers else []


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
  where is X defined/used; `impact` = what breaks if X changes. Pass
  `--symbol X` for locate/impact unless the question has one unambiguous
  identifier.
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
    """Apply the same lossless, ownership-scoped plan shown by ctx setup."""
    from ctx.config_edit import ConfigError
    from ctx.host_config import plan_configuration
    from ctx.mcp_hosts import IntegrationError

    plan = plan_configuration(ws.root, ["codex"])
    if plan.blockers:
        raise IntegrationError(plan.render())
    try:
        plan.apply()
    except ConfigError as exc:
        raise IntegrationError(str(exc)) from exc
    lines = [f"wrote {change.path}" for change in plan.changes]
    if not lines:
        lines.append("Codex configuration already harnessed; unchanged")
    if init_policy:
        lines.extend(init_workspace(ws.root, quiet=True))
    lines.append("remove: ctx setup --host codex --remove (preview); add --apply to remove owned contributions")
    return "\n".join(lines)


def _codex_hooks_contract(path: Path, exe: str) -> tuple[bool, str]:
    """All lifecycle stages, executable, matcher and timeout must be wired."""
    if path.is_symlink() or path.parent.is_symlink():
        return False, ".codex/hooks.json uses a symlink; setup refuses it"
    try:
        current = _read_settings_object(path)
    except (SettingsUnreadable, OSError, UnicodeError) as exc:
        return False, str(exc)
    expected = json.loads(_render_codex_file("hooks.json", exe))["hooks"]
    hooks = current.get("hooks")
    if not isinstance(hooks, dict):
        return False, ".codex/hooks.json is missing or has no hooks object; rerun ctx setup --host codex --preview"
    missing = [stage for stage, groups in expected.items()
               if not isinstance(hooks.get(stage), list)
               or any(group not in hooks[stage] for group in groups)]
    if missing:
        return False, "Codex hooks missing or changed: " + ", ".join(missing) + "; rerun ctx setup --host codex --preview"
    return True, ".codex/hooks.json verifies every ctx lifecycle stage"


def _hook_command_present_codex(settings: dict, ctx_exe: str) -> bool:
    # Same shape-tolerant traversal as the Claude reader: .codex/hooks.json is
    # foreign input too, and carried the identical unchecked assumptions.
    for cmd in _iter_hook_commands(settings):
        if " hook codex " in cmd:
            return True
    return False


# Host setup is explicit because each hook contract has different guarantees.
# Both of these are DERIVED from the host registry (ctx.hosts): the wired set
# and the name→installer mapping used to be hand-maintained here as well, a
# second copy of what every HostSpec already declares via `installer`.
def _setup_hosts_tuple() -> tuple[str, ...]:
    """The hosts `ctx setup` configures by default.

    Self-hosted hosts are excluded on purpose. Setup harnesses the agents you
    already have by writing config into them; building a virtualenv and pulling
    a vendor SDK off the network is a different kind of act, and it should be an
    explicit `ctx wrap antigravity-sdk`, never a side effect of `ctx setup`.
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
        "ctx harness setup — local context containment for coding agents",
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
        try:
            probe = store.put_blob(b"ctx-doctor-probe")
            store.get_blob(probe)
            check("store writable", True, store.location.detail)
        finally:
            # `ctx mcp` serves `op: "doctor"` from a long-lived process;
            # an unclosed Store per call is the leak _WS_CACHE exists for.
            store.close()
    except Exception as e:
        check("store writable", False, str(e))

    # Code intelligence is optional tooling, so a thin install is not a
    # failure — but it must never be invisible. Measuring ctx against a
    # missing universal-ctags and reporting the result as a ctx defect is a
    # mistake this row exists to make impossible.
    try:
        from ctx.skeleton import _ctags_path, backend_roster

        roster = backend_roster()
        dark = sorted(lang for lang, rungs in roster.items() if rungs == "none")
        detail = f"{len(roster) - len(dark)}/{len(roster)} languages parseable"
        if _ctags_path() is None:
            detail += "; no universal-ctags fallback"
        if dark:
            detail += f"; no backend for {', '.join(dark)}"
        check("code intelligence", len(dark) < len(roster), detail)
    except Exception as e:  # noqa: BLE001 — a probe that cannot run is a finding
        check("code intelligence", False, f"{type(e).__name__}: {e}")

    # The language-server tier (`ctx lsp`, the refs ladder's semantic rung)
    # is whatever servers are on PATH. Optional like the parsers, and for the
    # same reason never silent: a machine without one answers `ctx refs`
    # from jedi or the regex floor, and the header says so.
    try:
        from ctx.lsp import roster as lsp_roster

        servers = lsp_roster()
        found = {lang: exe for lang, exe in servers.items() if exe != "none"}
        detail = (
            f"{len(found)}/{len(servers)} languages: "
            + ", ".join(f"{lang}={exe}" for lang, exe in sorted(found.items()))
            if found else "no language server on PATH (ctx lsp unavailable; refs use jedi/regex)"
        )
        check("language servers", True, detail)
    except Exception as e:  # noqa: BLE001 — a probe that cannot run is a finding
        check("language servers", True, f"probe failed: {type(e).__name__}: {e}")

    # The text index (docs/CODE-SEARCH.md) is what search, the q search
    # stage and `ctx pack` narrow with. It builds itself on first use below a
    # size guard; above it, `ctx index --text` is one command, and a
    # workspace that needs it should be told so.
    try:
        from ctx import codeindex
        from ctx.store import Store as _Store

        _st = _Store(ws.workspace_id)
        try:
            _idx = codeindex.Index(_st, ws)
            _status = _idx.status()
            if _status["built"]:
                detail = f"{_status['files']} files · {_status['segments']} segment(s) · synced before every query"
            else:
                n, nbytes = _idx.corpus_estimate()
                big = n > codeindex.IMPLICIT_BUILD_MAX_FILES or nbytes > codeindex.IMPLICIT_BUILD_MAX_BYTES
                detail = (f"not built · {n} files; run ctx index --text (too large to build inside a query)"
                          if big else f"not built · {n} files; builds on first search")
            _idx.close()
        finally:
            _st.close()
        check("text index", True, detail)
    except Exception as e:  # noqa: BLE001 — a probe that cannot run is a finding
        check("text index", True, f"probe failed: {type(e).__name__}: {e}")

    # The exact tier of the refs/def ladders needs an index, and for years
    # nothing in ctx made one — so it was unreachable and every answer came
    # from the regex floor without saying so. Same rule as the row above:
    # optional, therefore not a failure; invisible, therefore a defect.
    try:
        from ctx import scip_index, scip_ingest

        have = scip_ingest.find_index(ws)
        if have is not None and scip_ingest.index_is_current(ws, have):
            check("exact index", True, "SCIP index present — refs/def answer exactly")
        elif have is not None:
            check(
                "exact index", True,
                "SCIP index is stale and is being skipped; refs/def have fallen "
                "back to the textual engine — run: ctx index",
            )
        else:
            wanted = scip_index.dominant_languages(ws)
            usable = [lang for lang in wanted if scip_index.available(lang)]
            detail = "no SCIP index; refs/def use the textual fallback"
            if usable:
                detail += f" — run: ctx index --language {usable[0]}"
            elif wanted:
                detail += f"; no indexer installed for {', '.join(wanted[:4])}"
            check("exact index", True, detail)
    except Exception as e:  # noqa: BLE001 — a probe that cannot run is a finding
        check("exact index", False, f"{type(e).__name__}: {e}")

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

    # What `ctx setup` actually wrote. Until now doctor never opened
    # these files, so it happily printed OK for a workspace where the hooks
    # had never been installed, or had been clobbered — the one question the
    # command exists to answer.
    wrapped: list[str] = []
    for label, rel, present_fn in (
        ("claude hooks", ".claude/settings.json", _hook_command_present),
    ):
        path = ws.root / rel
        if not path.is_file():
            continue
        try:
            data = _read_settings_object(path)
        except SettingsUnreadable as e:
            check(label, False, f"{e} — fix the file, then re-run: ctx setup")
            continue
        hooked = present_fn(data, exe or "")
        check(
            label,
            hooked,
            rel if hooked else f"{rel} has no ctx hook entry — run: ctx setup",
        )
        if hooked:
            wrapped.append(label.split()[0])
    codex_cfg = ws.root / ".codex" / "config.toml"
    codex_hooks = ws.root / ".codex" / "hooks.json"
    agents = ws.root / "AGENTS.md"
    try:
        agents_text = agents.read_text(encoding="utf-8") if agents.is_file() else ""
        has_codex_block = _CODEX_MARK_START in agents_text
    except (OSError, UnicodeError):
        agents_text = ""
        has_codex_block = False
    from ctx.host_config import owned_hosts
    if codex_cfg.exists() or codex_hooks.exists() or has_codex_block or "codex" in owned_hosts(ws.root):
        mcp_ok, mcp_detail = _codex_mcp_contract(codex_cfg, _ctx_executable())
        hooks_ok, hooks_detail = _codex_hooks_contract(codex_hooks, _ctx_executable())
        block_ok = (not agents.is_symlink() and agents_text.count(_CODEX_MARK_START) == 1
                    and agents_text.count(_CODEX_MARK_END) == 1
                    and agents_text.index(_CODEX_MARK_START) < agents_text.index(_CODEX_MARK_END))
        if block_ok:
            block = agents_text[agents_text.index(_CODEX_MARK_START):agents_text.index(_CODEX_MARK_END) + len(_CODEX_MARK_END)]
            block_ok = block == _render_codex_file("AGENTS.md", _ctx_executable()).strip()
        check("codex MCP", mcp_ok, mcp_detail)
        check("codex hooks", hooks_ok, hooks_detail)
        check("codex instructions", block_ok, "AGENTS.md managed block" if block_ok else "ctx block missing or changed in AGENTS.md; rerun ctx setup --host codex --preview")
        if mcp_ok and hooks_ok and block_ok:
            wrapped.append("codex")
    if plugin_dir.is_dir():
        wrapped.append("antigravity")
    from ctx.mcp_hosts import checks as mcp_checks
    from ctx.acp import checks as acp_checks

    for name, ok, detail in [*mcp_checks(ws.root), *acp_checks(ws.root)]:
        check(name, ok, detail)
        if ok:
            wrapped.append(name.split()[0])
    check(
        "an agent is wrapped",
        bool(wrapped),
        " + ".join(wrapped) if wrapped else "nothing is hooked yet — run: ctx setup",
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
            try:
                row = _store.db.execute(
                    "SELECT id FROM objects WHERE kind='run' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    manifest = _store.get_manifest(row[0])
                    jsonschema.validate(manifest, json.loads(schema_path.read_text(encoding="utf-8")))
                    check("manifest schema", True, "latest run manifest validates against invocation-v1")
            finally:
                _store.close()  # the third store doctor opened; the report missed this one
    except ImportError:
        pass
    except Exception as e:
        check("manifest schema", False, str(e)[:120])

    # Hook self-test: an unbounded whole-suite run must never reach the
    # terminal AS WRITTEN.
    #
    # This asserted `decision == "deny"`, which is one of the shapes that
    # satisfy that property, not the property itself. Under the replacement
    # surface a recognised loop-shape is ALLOWED and rewritten into the
    # collapsed `ctx q` op -- the flood is contained by substitution rather
    # than refusal -- so merely having captured one pytest failure earlier in
    # the session (an entirely normal thing to have done) made `ctx doctor`
    # report PROBLEMS FOUND while the guard was behaving exactly as designed.
    # A self-test that pins a proxy fails when the implementation finds a
    # better way to keep the promise.
    try:
        from ctx.hook import classify

        d = classify(
            {
                "tool_name": "run_command",
                "tool_input": {"CommandLine": "pytest -q", "Cwd": str(ws.root)},
                "workspacePaths": [str(ws.root)],
            }
        )
        verdict = str(d.get("decision"))
        # classify() returns the public decision contract. Internal steering
        # starts as ``_rewrite`` but is normalised to ``rewrite`` before the
        # result leaves ctx.hook; checking the private key made doctor report a
        # healthy collapsed rewrite as a failure after the first captured run.
        rewritten = isinstance(d.get("rewrite"), dict)
        contained = verdict in ("deny", "force_ask") or rewritten
        how = f"pytest → {verdict}" + (" + collapsed rewrite" if rewritten else "")
        check("hook classifier", contained, how)
    except Exception as e:
        check("hook classifier", False, str(e))

    # Cumulative savings from the telemetry ledger (operational, not identity).
    try:
        from ctx.retrieval import telemetry_summary
        from ctx.store import Store as _Store

        _tstore = _Store(ws.workspace_id)
        try:
            t = telemetry_summary(_tstore)
        finally:
            _tstore.close()
        if t["events"]:
            check(
                "telemetry",
                True,
                f"{t['events']} ops · est {t['est_tokens_avoided']:,} prompt tokens avoided",
            )
    except Exception:
        pass

    return checks


def install_hermes(ws, *, init_policy=True):
    from ctx.mcp_hosts import install
    return install("hermes", ws, init_policy=init_policy)


def install_omp(ws, *, init_policy=True):
    from ctx.mcp_hosts import install
    return install("omp", ws, init_policy=init_policy)


def install_opencode(ws, *, init_policy=True):
    from ctx.mcp_hosts import install
    return install("opencode", ws, init_policy=init_policy)


def install_dsh(ws, *, init_policy=True):
    from ctx.mcp_hosts import install
    return install("dsh", ws, init_policy=init_policy)


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
