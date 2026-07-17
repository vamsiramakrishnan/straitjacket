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


def _template_dir() -> Path:
    """Locate the packaged plugin template: repo checkout first, then a
    data dir next to the installed package."""
    here = Path(__file__).resolve()
    for candidate in (
        here.parent.parent.parent / "plugins" / "antigravity",  # src checkout
        here.parent / "data" / "antigravity",  # wheel data (future)
    ):
        if (candidate / "plugin.json").is_file():
            return candidate
    raise FileNotFoundError("plugin template not found; reinstall ctx-harness")


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

    skill_src = template / "skills"
    skill_dst = dest / "skills"
    if skill_dst.exists():
        shutil.rmtree(skill_dst)
    shutil.copytree(skill_src, skill_dst)
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
        dup = plugin_dir.is_dir() and skill_dir.is_dir()
        check(
            "no duplicate installation",
            not dup,
            "plugin and standalone skill are both installed — remove one (SPEC §4.3)" if dup else "",
        )

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

    ok_all = all(ok for _, ok, _ in checks)
    lines = [f"[ctx doctor v{__version__}] {'OK' if ok_all else 'PROBLEMS FOUND'}"]
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        lines.append(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return "\n".join(lines)
