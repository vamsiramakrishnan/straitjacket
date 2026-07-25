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
from ctx.proxywindow import PROXY_SUBDIR
from ctx.sessiondir import session_reads_path

_AGENT_FILENAME = "ctx-explorer.md"

# The Caveman lesson: retrieval discipline without emission discipline just
# moves tokens from tool results to narration. Injected in print mode only;
# opt out with CTX_WRAP_NO_DISCIPLINE=1 or by passing your own
# --append-system-prompt.
_OUTPUT_DISCIPLINE = (
    "Output discipline: narrate tersely. Never restate or quote file or tool "
    "output back into the conversation — cite coordinates instead (file:line, "
    "run:/blob: handles from ctx digests). Summaries are a few sentences, not "
    "a report; prefer acting over describing what you will do. "
    "Solution ladder — before writing any code, prefer in this order: "
    "(1) not needed at all, (2) reuse what already exists here, (3) the "
    "standard library, (4) a one-liner, (5) minimal new code. Be lazy about "
    "the solution, never about reading. If you deliberately defer an "
    "improvement, declare it in one line instead of building it. "
    "Backward planning: before your first action, state the final "
    "acceptance check (what command proves the task done), then the step "
    "immediately before it, and plan backward to your first action; then "
    "execute forward without re-planning."
)


# Orchestration belongs in the session, not in a command a human types.
# `ctx orchestrate "<task>"` makes routing something you invoke; nobody wants to
# stop and hand-route their own work. Wrapping with --orchestrate turns it into
# a *mode*: the session itself splits multi-step work across the installed
# models by cost, and the person just keeps working.
_ORCHESTRATION_MODE = (
    "Model routing is ON for this session. You have more than one model "
    "available; spend the cheapest one that can do each part. Before a "
    "multi-step task, split it: exploration, search, triage and verification "
    "go to an economy model; ordinary edits to a standard model; only "
    "architecture, planning and hard reasoning go to the flagship. Run "
    "`ctx wrap detect` to see which harnesses and models are installed with "
    "their prices, and `ctx orchestrate \"<task>\" --dry-run` to have the "
    "routing planned and priced for you. Hand work between steps as ctx "
    "handles (a checkpoint: or run:/blob:), never by pasting output. Do not "
    "ask the user to route work — routing is your job now."
)


def _with_output_discipline(agent_args: list[str], *, orchestrate: bool = False) -> list[str]:
    """Prepend the emission-discipline system prompt for print-mode runs."""
    if os.environ.get("CTX_WRAP_NO_DISCIPLINE"):
        return agent_args
    if "--append-system-prompt" in agent_args:
        return agent_args  # the user's own instruction wins
    if "-p" not in agent_args and "--print" not in agent_args:
        return agent_args  # interactive session: leave the human in charge
    prompt = _OUTPUT_DISCIPLINE
    if orchestrate:
        prompt = prompt + " " + _ORCHESTRATION_MODE
    return ["--append-system-prompt", prompt, *agent_args]


_NATIVE_SEARCH_TOOLS = ("Grep", "Glob")


def _collapse_enabled(workspace_root: Path) -> bool:
    """The replacement surface is the default posture — search is forced onto
    the doors the harness controls unless a workspace breaks glass with
    ``[guard] collapse = false`` in ctx.toml. Absent config → enabled.

    This was a third independent ``tomllib`` parse of ctx.toml, alongside
    the typed loader and the guard hot path. It now defers to the typed
    loader — which is already fail-open on a malformed file, and which
    ``tests/test_config_hook_parity.py`` pins against the hot path — so the
    only two readers left are the two with a reason to exist."""
    from ctx.config import load_config

    return bool(load_config(workspace_root).guard.collapse)


def _with_collapse_tool_removal(agent_args: list[str], workspace_root: Path) -> list[str]:
    """Under the replacement surface, remove Claude Code's native Grep/Glob
    tools so search is forced onto the doors the harness controls — Bash grep
    (transparently substituted) or the ctx verbs (already collapsed). No-op
    unless collapse is enabled, or if the caller already set --disallowedTools."""
    if "--disallowedTools" in agent_args or not _collapse_enabled(workspace_root):
        return agent_args
    return ["--disallowedTools", *_NATIVE_SEARCH_TOOLS, *agent_args]


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

    PreToolUse is the guard; PostToolUse is the emission governor + universal
    emission gate. Delegates to ``installer.claude_hook_settings`` so the
    ephemeral wrap and the persistent install share one source of truth."""
    from ctx.installer import claude_hook_settings

    return claude_hook_settings(ctx_exe)


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
    state_dir = session_reads_path(workspace_root, PROXY_SUBDIR)
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

        sc = compute_scorecard(session_reads_path(workspace_root, PROXY_SUBDIR))
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
    orchestrate: bool = False,
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
    agent_args = _with_output_discipline(agent_args, orchestrate=orchestrate)
    agent_args = _with_collapse_tool_removal(agent_args, workspace_root)
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
    for stage, entries in settings["hooks"].items():
        merged.setdefault("hooks", {}).setdefault(stage, []).extend(entries)
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


def wrap_codex(workspace_root: Path, ctx_exe: str | None = None) -> int:
    """Persistent install: Codex discovers .codex/ config layers + AGENTS.md
    from the workspace tree."""
    from ctx.installer import install_codex
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_root))
    print(install_codex(ws))
    print()
    print("Codex sessions in this workspace are now harnessed "
          "(MCP retrieval tool + PreToolUse/PostToolUse containment hooks).")
    return 0


def _fmt_price(dollars_per_mtok: float) -> str:
    """Compact per-1M-token dollar price for the detect table."""
    return f"${dollars_per_mtok:g}"


def render_detect_table(detected: list) -> str:
    """Deterministic table of every registered host: installed?, resolved
    model, price tier, and whether the harness can wrap it. Prices come from
    ctx.pricing so the same rows feed the cost-routing orchestrator."""
    from ctx.hosts import DetectedHost  # noqa: F401 (type reference only)

    rows: list[tuple[str, ...]] = []
    header = ("host", "installed", "model", "tier", "$in/$out per 1M", "wrap")
    for d in detected:
        installed = "yes" if d.installed else "no"
        wrap = "yes" if d.harnessable else "todo"
        price = f"{_fmt_price(d.price.input)}/{_fmt_price(d.price.output)}"
        rows.append(
            (d.name, installed, d.model, d.price.tier, price, wrap)
        )
    widths = [
        max(len(header[i]), *(len(r[i]) for r in rows)) if rows else len(header[i])
        for i in range(len(header))
    ]

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    out = ["ctx wrap detect — installed coding-agent CLIs, priced by model", ""]
    out.append(line(header))
    out.append(line(tuple("-" * w for w in widths)))
    out.extend(line(r) for r in rows)
    installed_wrappable = [d for d in detected if d.installed and d.harnessable]
    out.append("")
    if installed_wrappable:
        names = ", ".join(d.name for d in installed_wrappable)
        out.append(f"harnessable now: {names}")
        out.append("  ctx wrap setup           # configure the installed hosts")
        out.append("  ctx orchestrate \"<task>\"  # route work across them by cost")
    else:
        out.append(
            "no harnessable CLI detected on PATH — install one of: claude, "
            "codex, antigravity"
        )
    return "\n".join(out)


def wrap_detect(workspace_root: Path, *, probe_version: bool = False) -> int:
    """`ctx wrap detect`: probe PATH for every registered coding-agent CLI and
    print an installed/model/price table. This is the input to detection-driven
    setup and to the cost-routing orchestrator."""
    from ctx.hosts import detect_all

    detected = detect_all(workspace_root=workspace_root, probe_version=probe_version)
    print(render_detect_table(detected))
    return 0


def wrap_setup(
    workspace_root: Path, hosts: list[str] | None = None, *, force_all: bool = False
) -> int:
    """Single-command multi-host setup. By default this now *detects* which
    coding-agent CLIs are installed and configures exactly those (reporting the
    ones it skipped), instead of unconditionally writing config for all three.

    ``force_all`` (``ctx wrap all``/``--all``) restores the configure-everything
    behaviour; an explicit ``hosts`` list overrides detection entirely. When no
    harnessable CLI is found on PATH, setup falls back to configuring all
    supported hosts (config is inert until a CLI reads it) with a note."""
    from ctx.installer import SETUP_HOSTS, setup_hosts
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_root))

    if hosts is None and not force_all:
        from ctx.hosts import detect_all

        detected = detect_all(workspace_root=ws.root)
        installed = [d.name for d in detected if d.installed and d.harnessable]
        skipped = [
            d.name for d in detected if d.harnessable and not d.installed
        ]
        if installed:
            report = setup_hosts(ws, installed)
            print(report)
            if skipped:
                print()
                print(
                    "not on PATH, skipped: "
                    + ", ".join(skipped)
                    + "  (use `ctx wrap all` to configure them anyway)"
                )
            return 0
        # Nothing detected: configure all supported hosts so the workspace is
        # ready the moment a CLI is installed. Idempotent and non-destructive.
        print(
            "no coding-agent CLI detected on PATH; configuring all supported "
            f"hosts ({', '.join(SETUP_HOSTS)}) — config is inert until a CLI reads it.\n"
        )

    print(setup_hosts(ws, hosts))
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
    if host == "codex":
        from ctx.installer import _render_codex_file

        return "\n".join(
            [
                "# .codex/config.toml (MCP server + hooks feature):",
                _render_codex_file("config.toml", exe).rstrip(),
                "",
                "# .codex/hooks.json (PreToolUse/PostToolUse containment):",
                _render_codex_file("hooks.json", exe).rstrip(),
            ]
        )
    raise ValueError(
        f"unsupported wrap host {host!r} (expected claude|antigravity|codex)"
    )
