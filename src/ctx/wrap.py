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
    from ctx.installer import SettingsUnreadable, _read_settings_object

    path = workspace_root / ".claude" / "settings.json"
    original = path.read_bytes() if path.is_file() else None
    # The persistent install path already refuses a malformed settings.json
    # with a named, actionable error; this ephemeral path used to read the
    # same file with a bare json.loads and die of an unhandled
    # JSONDecodeError instead. Two readers of one foreign file, only one of
    # them hardened, is the defect -- so there is now one reader.
    try:
        merged: dict = _read_settings_object(path) if original else {}
    except SettingsUnreadable as e:
        print(
            f"ctx wrap claude: {e}\n"
            "Refusing to merge harness hooks into a settings file this run "
            "cannot safely restore. Fix or move the file, then retry.",
            file=sys.stderr,
        )
        return 2
    for stage, entries in settings["hooks"].items():
        bucket = merged.setdefault("hooks", {})
        if not isinstance(bucket, dict):
            print(
                "ctx wrap claude: .claude/settings.json has a 'hooks' value "
                f"that is a JSON {type(bucket).__name__}, not an object. "
                "Refusing to merge into it.",
                file=sys.stderr,
            )
            return 2
        bucket.setdefault(stage, []).extend(entries)
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


def wrap_agy_sdk(workspace_root: Path, ctx_exe: str | None = None) -> int:
    """Create and own the Antigravity SDK environment, then report honestly.

    Unlike the other wrappers this installs no hooks into anyone's config:
    there is no config to install into. The harnessing *is* the agent — its
    tools return bounded output by construction — so `ctx wrap antigravity-sdk`
    builds the venv, installs the SDK, and writes the `ctx-agy` launcher that
    :mod:`ctx.hosts` detects.
    """
    from ctx.agysdk import ensure_venv, launcher_path, venv_dir

    ok, msg = ensure_venv()
    print(msg)
    if not ok:
        print(
            "\nthe Antigravity SDK environment could not be built, so this host "
            "stays unavailable (the others are unaffected).\n"
            "  needs: network access and a working `python -m venv`\n"
            "  retry: ctx wrap antigravity-sdk",
            file=sys.stderr,
        )
        return 1
    print()
    print(f"launcher: {launcher_path()}")
    print(f"venv:     {venv_dir()}")
    print()
    print("This host is ctx's own Antigravity agent, not Google's `agy` CLI.")
    print("  why:  `agy` is OAuth-only (nothing can script it) and its hook")
    print("        contract can substitute neither tool input nor tool output,")
    print("        so it has no output-side gate — see spec/adr/005.")
    print("  here: containment lives in the tool implementations, so output is")
    print("        bounded before the model ever sees it. Needs GEMINI_API_KEY.")
    print()
    print("Your `agy` install is untouched; `ctx wrap antigravity` still harnesses it.")
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


def _guided_survey(ws) -> tuple[list, list, list]:
    """(will harness, skipped-not-installed, optional-not-installed).

    `optional` are hosts ctx would have to *build* rather than detect — they are
    never configured implicitly, so they are offered rather than done.
    """
    from ctx.hosts import detect_all

    detected = [d for d in detect_all(workspace_root=ws.root) if d.harnessable]
    will = [d for d in detected if d.installed]
    skipped = [d for d in detected if not d.installed and not d.spec.self_hosted]
    optional = [d for d in detected if not d.installed and d.spec.self_hosted]
    return will, skipped, optional


def _short_path(path: str | None, width: int = 34) -> str:
    """Keep the survey table aligned: a long managed-venv path is elided in the
    middle, where the uninformative part lives."""
    p = path or ""
    if len(p) <= width:
        return p
    keep = (width - 1) // 2
    return p[:keep] + "…" + p[-(width - keep - 1):]


def _guided_step(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")
    print("─" * (len(title) + 6))


def guided_setup(ws, hosts: list[str] | None = None, *, force_all: bool = False) -> int:
    """`ctx wrap setup`, narrated: survey → harness → verify → what next.

    The old flow printed each installer's output and stopped, which left a
    developer with a wall of paths and no answer to "did that work, and what do
    I do now?". Every step here is the same machinery as before; what is new is
    that the run says what it is about to do, checks its own work with the
    doctor's checks, and ends with one concrete next action — including when
    something failed.
    """
    from ctx.installer import SETUP_HOSTS, doctor_checks, setup_hosts

    will, skipped, optional = _guided_survey(ws)
    explicit = hosts is not None or force_all

    # ---------------------------------------------------------------- survey
    _guided_step(1, 4, "What you have")
    if explicit:
        names = list(hosts) if hosts else list(SETUP_HOSTS)
        print(f"  configuring on request: {', '.join(names)}")
        print("  (config is inert until a CLI reads it, so this is safe to do early)")
    elif will:
        for d in will:
            print(f"  ✓ {d.name:<16} {_short_path(d.path):<34} will harness")
        for d in skipped:
            print(f"  ✗ {d.name:<16} {'not on PATH':<34} skipped")
        for d in optional:
            print(f"  ○ {d.name:<16} {'not installed':<34} optional — "
                  f"`ctx wrap {d.name}`")
    else:
        print("  no coding-agent CLI found on PATH.")
        print(f"  configuring all supported hosts anyway ({', '.join(SETUP_HOSTS)}) —")
        print("  the config is inert until a CLI reads it, so installing one later")
        print("  needs no second setup.")

    # --------------------------------------------------------------- harness
    _guided_step(2, 4, "Harnessing")
    target = (list(hosts) if hosts is not None
              else ([d.name for d in will] if (will and not force_all) else None))
    # Indented so the per-host detail reads as evidence *under* this step
    # rather than as the whole output. It is kept in full on purpose: these
    # lines name every file written, which is what makes the undo note true.
    for line in setup_hosts(ws, target).splitlines():
        print(f"  {line}" if line.strip() else "")

    # ---------------------------------------------------------------- verify
    _guided_step(3, 4, "Verifying")
    checks = doctor_checks(ws)
    failed = [(n, d) for n, ok, d in checks if not ok]
    for name, ok, detail in checks:
        if not ok:
            print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
    if failed:
        print(f"  {len(checks) - len(failed)}/{len(checks)} checks passed.")
        print("  the failures above are the whole story; `ctx doctor` re-runs them.")
    else:
        print(f"  ✓ all {len(checks)} checks passed  (same checks as `ctx doctor`)")

    # ------------------------------------------------------------- next step
    _guided_step(4, 4, "What now")
    if failed:
        print("  fix the checks above first — until then containment is partial.")
        print("  most common cause: `ctx` not on PATH for the agent's environment.")
    else:
        print("  Nothing else to do. Your agent is harnessed from its next session.")
    print()
    print("  see it work now (no agent needed):")
    print("      ctx run -- <any noisy command, e.g. your test suite>")
    print("  then, at any point:")
    print("      ctx gain      what it kept out of your context, and what that saved")
    print("      ctx doctor    re-check the install")
    if optional and not explicit:
        print()
        print("  optional, only if you want it:")
        for d in optional:
            print(f"      ctx wrap {d.name}   headless Gemini agent, ctx builds its venv (~40s)")
    print()
    print("  undo: the per-host lines above name every file written; removing the")
    print("        ctx entries from them fully uninstalls. Nothing else was touched.")
    return 1 if failed else 0


def wrap_setup(
    workspace_root: Path, hosts: list[str] | None = None, *, force_all: bool = False
) -> int:
    """Single-command multi-host setup. By default this now *detects* which
    coding-agent CLIs are installed and configures exactly those (reporting the
    ones it skipped), instead of unconditionally writing config for all three.

    ``force_all`` (``ctx wrap all``/``--all``) restores the configure-everything
    behaviour; an explicit ``hosts`` list overrides detection entirely. When no
    harnessable CLI is found on PATH, setup falls back to configuring all
    supported hosts (config is inert until a CLI reads it) with a note.

    Output is guided by default (survey → harness → verify → next step); set
    ``CTX_SETUP_PLAIN=1`` for the bare installer report, which is what scripts
    that parse this output want."""
    from ctx.installer import SETUP_HOSTS, setup_hosts
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_root))

    if os.environ.get("CTX_SETUP_PLAIN") != "1":
        return guided_setup(ws, hosts, force_all=force_all)

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
