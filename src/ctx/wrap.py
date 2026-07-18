"""Zero-friction onboarding: `ctx wrap <host>` runs an agent under the harness.

Claude Code is wrapped ephemerally — hooks are passed via a temporary
``--settings`` file, the ctx-explorer agent definition is installed into
``.claude/agents/`` for the session, and nothing persists after exit.
Antigravity discovers plugins from the workspace, so wrapping it delegates
to the persistent installer.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ctx.installer import _ctx_executable

_HOOK_STAGE = "hook claude-code pre-tool-use"
_AGENT_FILENAME = "ctx-explorer.md"

# The Caveman lesson: retrieval discipline without emission discipline just
# moves tokens from tool results to narration. Injected in print mode only;
# opt out with CTX_WRAP_NO_DISCIPLINE=1 or by passing your own
# --append-system-prompt.
_OUTPUT_DISCIPLINE = (
    "Output discipline: narrate tersely. Never restate or quote file or tool "
    "output back into the conversation — cite coordinates instead (file:line, "
    "run:/blob: handles from ctx digests). Summaries are a few sentences, not "
    "a report; prefer acting over describing what you will do."
)


def _with_output_discipline(agent_args: list[str]) -> list[str]:
    """Prepend the emission-discipline system prompt for print-mode runs."""
    if os.environ.get("CTX_WRAP_NO_DISCIPLINE"):
        return agent_args
    if "--append-system-prompt" in agent_args:
        return agent_args  # the user's own instruction wins
    if "-p" not in agent_args and "--print" not in agent_args:
        return agent_args  # interactive session: leave the human in charge
    return ["--append-system-prompt", _OUTPUT_DISCIPLINE, *agent_args]


def _explorer_agent_source() -> Path:
    """The packaged explorer agent definition (shipped with the plugin)."""
    from ctx.installer import _template_dir

    return _template_dir() / "agents" / _AGENT_FILENAME


def _install_explorer_agent(workspace_root: Path) -> Path | None:
    """Write the explorer agent into ``.claude/agents/`` for the session.

    Returns the created path, or None when a file already exists there —
    a user's own agent definition is never touched."""
    dest = workspace_root / ".claude" / "agents" / _AGENT_FILENAME
    if dest.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_explorer_agent_source().read_bytes())
    return dest


def _remove_explorer_agent(created: Path | None) -> None:
    """Undo _install_explorer_agent: remove the file we wrote and any
    directories we created that are now empty — zero residue."""
    if created is None:
        return
    created.unlink(missing_ok=True)
    for parent in (created.parent, created.parent.parent):  # agents/, .claude/
        with contextlib.suppress(OSError):
            parent.rmdir()  # only succeeds when empty


def prepare_claude(workspace_root: Path, ctx_exe: str) -> dict:
    """Claude Code settings dict that routes tool calls through the harness.

    PreToolUse is the guard; PostToolUse is the emission governor
    (mechanism B) — it injects a terse one-line nudge when the proxy-measured
    cumulative output crosses a pressure tier, and stays silent otherwise."""
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
            ],
            "PostToolUse": [
                {
                    "matcher": "Bash|Read|Edit|Write",
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _start_proxy(
    workspace_root: Path, ctx_exe: str, rescue_pct: float = 0.0
) -> tuple[subprocess.Popen | None, dict[str, str] | None]:
    """Spawn the Tier-0 observer proxy and return (process, child env).

    The child env carries ANTHROPIC_BASE_URL pointed at the local proxy;
    the parent process env is never modified. Fail-open: if the proxy does
    not come up within 5s, the session runs unproxied."""
    port = _free_port()
    upstream = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    state_dir = workspace_root / ".ctx-session-reads" / "proxy"
    argv = [
        *shlex.split(ctx_exe),
        "proxy",
        "--port", str(port),
        "--upstream", upstream,
        "--state-dir", str(state_dir),
    ]
    if rescue_pct > 0:
        argv += ["--rescue-pct", str(rescue_pct)]
    proc = subprocess.Popen(argv, cwd=workspace_root)
    if not _wait_for_port(port, 5.0):
        _stop_proxy(proc)
        print("ctx wrap: observer proxy failed to start; continuing without it", file=sys.stderr)
        return None, None
    return proc, {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}


def _stop_proxy(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _emit_scorecard(workspace_root: Path) -> None:
    """Session-end economics from wire ground truth (mechanism D). Printed
    to stderr and appended to scorecard history for the policy learner.
    Fail-open: a scorecard problem never affects the session's exit."""
    try:
        from ctx.scorecard import (
            append_history,
            attach_deliverable,
            compute_scorecard,
            summary_line,
        )

        sc = compute_scorecard(workspace_root / ".ctx-session-reads" / "proxy")
        if sc is None:
            return
        attach_deliverable(sc, workspace_root)
        append_history(workspace_root, sc)
        print(summary_line(sc), file=sys.stderr)
    except Exception:
        pass


def _claude_supports_settings(claude: str) -> bool:
    try:
        proc = subprocess.run([claude, "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--settings" in (proc.stdout + proc.stderr)


def wrap_claude(
    workspace_root: Path,
    agent_args: list[str],
    ctx_exe: str | None = None,
    use_proxy: bool = False,
    rescue_pct: float = 0.0,
) -> int:
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

    exe = ctx_exe or _ctx_executable()
    agent_args = _with_output_discipline(agent_args)
    settings = prepare_claude(workspace_root, exe)
    # The explorer agent lives alongside the hooks for the session's lifetime.
    agent_file = _install_explorer_agent(workspace_root)
    proxy_proc: subprocess.Popen | None = None
    child_env: dict[str, str] | None = None
    try:
        if use_proxy:
            proxy_proc, child_env = _start_proxy(workspace_root, exe, rescue_pct)
        if not _claude_supports_settings(claude):
            print(
                "ctx wrap: this claude build lacks --settings; "
                "temporarily merging into .claude/settings.json (restored on exit)",
                file=sys.stderr,
            )
            return _wrap_claude_merged(workspace_root, settings, claude, agent_args, child_env)

        tmp = tempfile.NamedTemporaryFile(
            "w", prefix="ctx-wrap-", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            json.dump(settings, tmp)
            tmp.close()
            # Inherit stdio so interactive sessions work.
            proc = subprocess.run(
                [claude, "--settings", tmp.name, *agent_args],
                cwd=workspace_root,
                env=child_env,
            )
            return proc.returncode
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
    finally:
        _stop_proxy(proxy_proc)
        if proxy_proc is not None:
            _emit_scorecard(workspace_root)
        _remove_explorer_agent(agent_file)


def _wrap_claude_merged(
    workspace_root: Path,
    settings: dict,
    claude: str,
    agent_args: list[str],
    child_env: dict[str, str] | None = None,
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
        return subprocess.run(
            [claude, *agent_args], cwd=workspace_root, env=child_env
        ).returncode
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
