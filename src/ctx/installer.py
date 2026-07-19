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


def install_antigravity(ws: Workspace, *, init_policy: bool = True) -> str:
    dest = render_plugin(ws.root)
    lines = [f"installed plugin: {dest}"]
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
        }
    }


def _hook_command_present(settings: dict, ctx_exe: str) -> bool:
    """True if a ctx-harness hook command is already registered."""
    for stage in settings.get("hooks", {}).values():
        for group in stage:
            for hook in group.get("hooks", []):
                cmd = str(hook.get("command", ""))
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
    existing: dict = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    if _hook_command_present(existing, exe):
        lines.append(".claude/settings.json already harnessed; left unchanged")
    else:
        merged = dict(existing)
        for stage, entries in claude_hook_settings(exe)["hooks"].items():
            merged.setdefault("hooks", {}).setdefault(stage, []).extend(entries)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        lines.append(f"wrote {settings_path.relative_to(root)} (hooks merged)")

    agent_src = _template_dir() / "agents" / "ctx-explorer.md"
    agent_dst = root / ".claude" / "agents" / "ctx-explorer.md"
    if agent_dst.exists():
        lines.append(".claude/agents/ctx-explorer.md exists; left unchanged")
    elif agent_src.is_file():
        agent_dst.parent.mkdir(parents=True, exist_ok=True)
        agent_dst.write_bytes(agent_src.read_bytes())
        lines.append(f"wrote {agent_dst.relative_to(root)}")

    if init_policy:
        lines.extend(init_workspace(root, quiet=True))
    lines.append("uninstall: remove the ctx hook entries from .claude/settings.json")
    return "\n".join(lines)


_CODEX_MARK_START = "<!-- ctx-harness:start -->"
_CODEX_MARK_END = "<!-- ctx-harness:end -->"


def _render_codex_file(name: str, exe: str) -> str:
    tmpl = _template_dir("codex", sentinel="config.toml")
    return (tmpl / name).read_text(encoding="utf-8").replace("{{CTX_EXECUTABLE}}", exe)


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
            cur = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cur = {}
        if _hook_command_present_codex(cur, exe):
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
    for stage in settings.get("hooks", {}).values():
        for group in stage:
            for hook in group.get("hooks", []):
                if " hook codex " in str(hook.get("command", "")):
                    return True
    return False


# The harness is built for Antigravity and works with Claude Code and Codex.
_HOST_INSTALLERS = {
    "antigravity": install_antigravity,
    "claude": install_claude,
    "codex": install_codex,
}
SETUP_HOSTS = ("antigravity", "claude", "codex")


def setup_hosts(ws: Workspace, hosts: "list[str] | None" = None) -> str:
    """Single-command multi-host setup. Configures the harness for each named
    host (default: all three) and returns a combined per-host report."""
    selected = hosts or list(SETUP_HOSTS)
    out: list[str] = [
        "ctx harness setup — built for Antigravity, works with Claude Code and Codex",
        "",
    ]
    for host in selected:
        installer = _HOST_INSTALLERS.get(host)
        out.append(f"── {host} ──")
        if installer is None:
            out.append(f"unknown host {host!r} (choose from {', '.join(SETUP_HOSTS)})")
        else:
            # Each host inits policy once; keep it quiet after the first.
            out.append(installer(ws, init_policy=(host == selected[0])))
        out.append("")
    out.append("validate: ctx doctor --antigravity   (and inspect .claude/ / .codex/)")
    return "\n".join(out).rstrip() + "\n"


def doctor_report(ws: Workspace, *, antigravity: bool = False) -> str:
    """Health checks per SPEC §18: discovery, JSON validity, duplication,
    store access, workspace resolution, hook self-test."""
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

    ok_all = all(ok for _, ok, _ in checks)
    lines = [f"[ctx doctor v{__version__}] {'OK' if ok_all else 'PROBLEMS FOUND'}"]
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        lines.append(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return "\n".join(lines)
