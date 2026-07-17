"""Zero-friction onboarding: `ctx wrap <host>` runs an agent under the harness.

Claude Code is wrapped ephemerally — hooks are passed via a temporary
``--settings`` file and nothing persists after the session. Antigravity
discovers plugins from the workspace, so wrapping it delegates to the
persistent installer.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ctx.installer import _ctx_executable

_HOOK_STAGE = "hook claude-code pre-tool-use"


def prepare_claude(workspace_root: Path, ctx_exe: str) -> dict:
    """Claude Code settings dict that routes tool calls through the harness."""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Read",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{ctx_exe} {_HOOK_STAGE}",
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
    }


def _claude_supports_settings(claude: str) -> bool:
    try:
        proc = subprocess.run([claude, "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--settings" in (proc.stdout + proc.stderr)


def wrap_claude(workspace_root: Path, agent_args: list[str], ctx_exe: str | None = None) -> int:
    """Launch `claude` with harness hooks injected; leave zero residue."""
    claude = shutil.which("claude")
    if claude is None:
        print(
            "ctx wrap: `claude` not found on PATH.\n"
            "  install Claude Code (npm install -g @anthropic-ai/claude-code)\n"
            "  or add it to PATH, then re-run: ctx wrap claude",
            file=sys.stderr,
        )
        return 127

    settings = prepare_claude(workspace_root, ctx_exe or _ctx_executable())
    if not _claude_supports_settings(claude):
        print(
            "ctx wrap: this claude build lacks --settings; "
            "temporarily merging into .claude/settings.json (restored on exit)",
            file=sys.stderr,
        )
        return _wrap_claude_merged(workspace_root, settings, claude, agent_args)

    tmp = tempfile.NamedTemporaryFile(
        "w", prefix="ctx-wrap-", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(settings, tmp)
        tmp.close()
        # Inherit stdio so interactive sessions work.
        proc = subprocess.run([claude, "--settings", tmp.name, *agent_args], cwd=workspace_root)
        return proc.returncode
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)


def _wrap_claude_merged(
    workspace_root: Path, settings: dict, claude: str, agent_args: list[str]
) -> int:
    """Fallback for claude builds without --settings: merge hooks into the
    workspace settings file, run, then restore the previous state exactly."""
    path = workspace_root / ".claude" / "settings.json"
    original = path.read_bytes() if path.is_file() else None
    merged: dict = json.loads(original) if original else {}
    merged.setdefault("hooks", {}).setdefault("PreToolUse", []).extend(
        settings["hooks"]["PreToolUse"]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        return subprocess.run([claude, *agent_args], cwd=workspace_root).returncode
    finally:
        if original is None:
            path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                path.parent.rmdir()  # only if we created it and it is empty
        else:
            path.write_bytes(original)


def wrap_antigravity(workspace_root: Path, ctx_exe: str | None = None) -> int:
    """Persistent install: Antigravity discovers plugins from the workspace."""
    from ctx.installer import install_antigravity
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_root))
    print(install_antigravity(ws))
    print()
    print("Antigravity sessions in this workspace are now harnessed.")
    print(
        "note: this install is persistent (Antigravity discovers plugins from "
        "the workspace tree); `ctx wrap claude` is ephemeral by contrast — "
        "remove .agents/plugins/ctx-harness to uninstall."
    )
    return 0


def print_config(host: str, ctx_exe: str | None = None) -> str:
    """Copy-pasteable configuration for a host, for CI and docs."""
    exe = ctx_exe or _ctx_executable()
    if host == "claude":
        return json.dumps(prepare_claude(Path.cwd(), exe), indent=2)
    if host == "antigravity":
        return "\n".join(
            [
                "# Render the repo-scoped plugin (persistent, workspace-discovered):",
                "ctx antigravity install --scope workspace --workspace .",
                "# Validate:",
                "ctx doctor --antigravity",
            ]
        )
    raise ValueError(f"unsupported wrap host {host!r} (expected claude|antigravity)")
