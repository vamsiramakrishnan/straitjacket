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

# evals/bugbash-round17-2026-09-04.md: a harnessed main agent delegated a bug
# hunt to 7 background subagents, then called the built-in ScheduleWakeup
# tool and ended its turn on the strength of its tool result ("the harness
# re-invokes you when the wakeup fires") — a claim that is only true in an
# interactive session. In `claude -p`, ending the turn ends the run, and the
# subagents' work is lost. Not tracked as a prefix asset (see
# tests/test_prefix_stability.py): it rides the same injection point and
# opt-outs as _OUTPUT_DISCIPLINE without being pinned by that manifest.
_SINGLE_SHOT_NOTICE = (
    "This is a single-shot, non-interactive run: there is no supervisor. "
    "Once this turn ends, nothing brings you back — no wakeup, no "
    "notification, no timer — even if a tool result says otherwise. If you "
    "delegate work to background subagents, stay in this turn and collect "
    "their results before you finish. Prefer running subagents in the "
    "foreground: every blocking wait on background work spends one of your "
    "turns, and a turn cap can run out before the work is collected. Ending "
    "your turn to wait for something to notify you later is the one thing "
    "you must never do here."
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
    prompt = _OUTPUT_DISCIPLINE + " " + _SINGLE_SHOT_NOTICE
    if orchestrate:
        prompt = prompt + " " + _ORCHESTRATION_MODE
    return ["--append-system-prompt", prompt, *agent_args]


_PRINT_BG_WAIT_CEILING_VAR = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"


def _with_print_bg_wait_ceiling(
    agent_args: list[str], env: dict[str, str] | None
) -> dict[str, str] | None:
    """In print mode, Claude Code kills background subagents a fixed ceiling
    (600 s) after the main turn ends. evals/bugbash-round17-2026-09-04.md: a
    harnessed run's main agent delegated to 7 background agents, ended its
    turn to wait for their notifications, and print mode killed all 7 at the
    ceiling before any reported back -- zero findings, $22 spent. A wrapped
    print-mode run is a delegated task whose subagents' work is the
    deliverable; discarding it on a timer is never what the caller wanted.
    Default to waiting indefinitely (0); never touch a ceiling the user set."""
    if "-p" not in agent_args and "--print" not in agent_args:
        return env  # interactive session: unrelated to this ceiling
    base = env if env is not None else os.environ
    if _PRINT_BG_WAIT_CEILING_VAR in base:
        return env  # the user's own setting wins
    return {**base, _PRINT_BG_WAIT_CEILING_VAR: "0"}


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


#: The tool surface of a headless coding run. Measured on this host: the
#: default catalogue is 16 tools / 154 KB (Artifact alone 71 KB, Agent 16 KB,
#: Workflow 9 KB, ScheduleWakeup 8 KB ...) and costs ~33k cached tokens on
#: EVERY request; these eight are 36-50 KB. Claude Code drops a tool that is
#: not in `--tools` from the prompt entirely, so this is the one prefix lever
#: that loses nothing the model would read: a coding task never calls
#: Artifact or ScheduleWakeup, and the explorer agent still rides `Agent`.
#: Interactive sessions keep everything (the human may want any tool); a
#: caller's own `--tools` wins; CTX_WRAP_NO_TOOL_DIET=1 opts out.
_PRINT_TOOL_SURFACE = ("Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "Agent")


def _with_tool_diet(agent_args: list[str]) -> list[str]:
    """Declare the coding tool surface for print-mode runs (see above)."""
    if os.environ.get("CTX_WRAP_NO_TOOL_DIET"):
        return agent_args
    if "--tools" in agent_args:
        return agent_args  # the caller's own surface wins
    if "-p" not in agent_args and "--print" not in agent_args:
        return agent_args  # interactive: leave the human every tool
    return ["--tools", ",".join(_PRINT_TOOL_SURFACE), *agent_args]


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


_FIRST_PARTY_HOST_SUFFIX = ".anthropic.com"


def _first_party_upstream(upstream: str) -> bool:
    """Is the proxy relaying to Anthropic's own API (as opposed to a user's
    gateway, Bedrock/Vertex shim, or a test double)?"""
    try:
        from urllib.parse import urlparse

        host = (urlparse(upstream).hostname or "").lower()
    except Exception:
        return False
    return host == "api.anthropic.com" or host.endswith(_FIRST_PARTY_HOST_SUFFIX)


def _proxy_child_env(port: int, upstream: str) -> dict[str, str]:
    """The environment the wrapped agent runs in when the observer proxy is
    on. ANTHROPIC_BASE_URL points at the loopback relay; the parent process
    env is never modified.

    Claude Code treats any non-first-party ANTHROPIC_BASE_URL as a gateway
    that may not forward its `tool_reference` beta, and silently turns
    deferred tool loading off. Measured on DeepSWE (evals/agentbench,
    haiku, 60-turn sessions): the prompt went from 16 deferred-loaded tool
    schemas to 41 inline ones, about 100 KB and ~15k cached tokens on EVERY
    request, a 21-33% input-token tax that dwarfed anything the hooks
    saved. The relay forwards headers and bodies byte-for-byte, so when the
    upstream is Anthropic itself the beta works through it; say so with
    ENABLE_TOOL_SEARCH (the CLI's own override) unless the user already
    chose a value, and leave a user's real gateway alone."""
    env = {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}
    if _first_party_upstream(upstream) and not os.environ.get("ENABLE_TOOL_SEARCH"):
        env["ENABLE_TOOL_SEARCH"] = "true"
    return env


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
    return proc, _proxy_child_env(port, upstream)


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
    agent_args = _with_tool_diet(agent_args)
    agent_args = _with_collapse_tool_removal(agent_args, workspace_root)
    settings = prepare_claude(workspace_root, exe)
    # The explorer agent lives alongside the hooks for the session's lifetime.
    agent_file = _install_explorer_agent(workspace_root)
    proxy_proc: subprocess.Popen | None = None
    child_env: dict[str, str] | None = None
    try:
        if use_proxy:
            proxy_proc, child_env = _start_proxy(workspace_root, exe, rescue_pct)
        child_env = _with_print_bg_wait_ceiling(agent_args, child_env)
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
    from ctx.installer import (
        SettingsUnreadable,
        _read_settings_object,
        merge_hook_stages,
    )

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
    # Third caller of one merge. The shape guard used to be inlined here and
    # in install_claude, and install_codex -- which does the same merge -- had
    # neither, so a bug bash crashed it on the shape both others handled.
    try:
        merge_hook_stages(merged, settings["hooks"])
    except SettingsUnreadable as e:
        print(
            f"ctx wrap claude: .claude/settings.json — {e}. "
            "Refusing to merge into it.",
            file=sys.stderr,
        )
        return 2
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
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_root))
    # Use the same preflight + installer + doctor path as `ctx setup`.  The
    # old direct path printed "now harnessed" even when a user-owned TOML was
    # left incomplete, then returned success.  guided_setup names the reviewed
    # edit, verifies the result, and propagates a non-zero status.
    return guided_setup(ws, hosts=["codex"])


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
        tier = d.price.tier
        if d.model == "unknown":
            price, tier = "unknown", "unknown"
        rows.append(
            (d.name, installed, d.model, tier, price, wrap)
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
        out.append("  ctx setup           # configure the installed hosts")
        if any(d.spec.unattended for d in installed_wrappable):
            out.append("  ctx orchestrate \"<task>\"  # route across eligible unattended workers")
        out.append("  MCP-only integrations provide explicit tools, not orchestration workers")
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
    # `p[-n:]` is the whole string at n == 0, so a width small enough to leave
    # no room for a tail returned the FULL path from the function whose job is
    # to shorten it -- the elision widening its own output. Same class as
    # `ctx job --tail 0` dumping the spool, in a display helper.
    tail = max(0, width - keep - 1)
    return p[:keep] + "…" + (p[len(p) - tail :] if tail else "")


def _guided_step(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")
    print("─" * (len(title) + 6))


def guided_setup(
    ws,
    hosts: list[str] | None = None,
    *,
    force_all: bool = False,
    force_repair: bool = False,
) -> int:
    """`ctx setup`, narrated: survey → harness → verify → what next.

    The old flow printed each installer's output and stopped, which left a
    developer with a wall of paths and no answer to "did that work, and what do
    I do now?". Every step here is the same machinery as before; what is new is
    that the run says what it is about to do, checks its own work with the
    doctor's checks, and ends with one concrete next action — including when
    something failed.
    """
    from ctx.installer import SETUP_HOSTS, doctor_checks, setup_conflicts, setup_hosts
    from ctx import __version__
    from ctx.setup_policy import choose_setup
    from ctx.setup_telemetry import (
        load_setup_receipt,
        record_setup,
        setup_is_current,
    )

    started = time.perf_counter()
    will, skipped, optional = _guided_survey(ws)
    explicit = hosts is not None or force_all
    target = (list(hosts) if hosts is not None
              else ([d.name for d in will] if (will and not force_all)
                    else list(SETUP_HOSTS)))
    if not explicit and not will:
        # Hermes needs its installed profile writer. An implicit inert recipe
        # would poison doctor checks for every later host setup.
        target = [name for name in target if name != "hermes"]
    prior = load_setup_receipt(ws.root)
    conflicts = setup_conflicts(ws, target)
    strategy = choose_setup(
        {
            "unmanaged_conflict": bool(conflicts),
            "receipt_current": setup_is_current(ws.root, target),
            "force_repair": force_repair,
            "had_receipt": prior is not None,
            "explicit": explicit,
            "installed_hosts": [d.name for d in will],
        }
    )

    if strategy == "refuse_unmanaged":
        print("ctx setup — one reviewed edit needed")
        for conflict in conflicts:
            for line in conflict.splitlines():
                print(f"  {line}")
        print("  managed files were not changed")
        record_setup(
            ws.root,
            target,
            strategy=strategy,
            success=False,
            checks_total=1,
            checks_passed=0,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return 1

    if strategy == "ready_noop":
        elapsed = (time.perf_counter() - started) * 1000
        print("ctx setup — already ready")
        print(
            f"  ✓ ctx {__version__}; "
            f"{', '.join(target)}; managed config unchanged"
        )
        print("  next: start your agent (use `ctx setup --repair` to force verification)")
        from ctx.mcp_hosts import HOSTS, next_step
        for host in target:
            if host in HOSTS:
                print("  " + next_step(host, ws.root))
        record_setup(
            ws.root,
            target,
            strategy=strategy,
            success=True,
            checks_total=int(prior.get("checks_total", 0)) if prior else 0,
            checks_passed=int(prior.get("checks_passed", 0)) if prior else 0,
            duration_ms=elapsed,
        )
        return 0

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
        print(f"  preparing project-local integrations ({', '.join(target)}) —")
        print("  Hermes needs its installed CLI; run `ctx setup --host hermes` after installation.")
        print("  the config is inert until a CLI reads it, so installing one later")
        print("  needs no second setup.")

    # --------------------------------------------------------------- harness
    _guided_step(2, 4, "Harnessing")
    # Indented so the per-host detail reads as evidence *under* this step
    # rather than as the whole output. It is kept in full on purpose: these
    # lines name every file written, which is what makes the undo note true.
    from ctx.mcp_hosts import IntegrationError

    try:
        setup_report = setup_hosts(ws, target)
    except (IntegrationError, OSError) as exc:
        print(f"  setup incomplete: {exc}", file=sys.stderr)
        record_setup(ws.root, target, strategy=strategy, success=False,
                     checks_total=1, checks_passed=0,
                     duration_ms=(time.perf_counter() - started) * 1000)
        return 1
    for line in setup_report.splitlines():
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
        print("  Configuration checks passed. Follow the host's start instructions above.")
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
    print("  undo: remove only the ctx-owned entries and blocks named above; see")
    print("        Troubleshooting for status-line and ownership checks. Captured")
    print("        artifacts and the content-free setup receipt may remain.")
    record_setup(
        ws.root,
        target,
        strategy=strategy,
        success=not failed,
        checks_total=len(checks),
        checks_passed=len(checks) - len(failed),
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return 1 if failed else 0


def wrap_setup(
    workspace_root: Path,
    hosts: list[str] | None = None,
    *,
    force_all: bool = False,
    force_repair: bool = False,
) -> int:
    """Single-command multi-host setup. By default this now *detects* which
    coding-agent CLIs are installed and configures exactly those (reporting the
    ones it skipped), instead of unconditionally writing config for all three.

    ``force_all`` (``ctx wrap all``/``--all``) restores the configure-everything
    behaviour; an explicit ``hosts`` list overrides detection entirely. When no
    harnessable CLI is found on PATH, setup prepares project-local integrations
    with a note. Hermes waits for its installed profile writer.

    Output is guided by default (survey → harness → verify → next step); set
    ``CTX_SETUP_PLAIN=1`` for the bare installer report, which is what scripts
    that parse this output want."""
    from ctx.installer import SETUP_HOSTS, setup_hosts
    from ctx.mcp_hosts import IntegrationError
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_root))

    if os.environ.get("CTX_SETUP_PLAIN") != "1":
        return guided_setup(
            ws, hosts, force_all=force_all, force_repair=force_repair
        )

    if hosts is None and not force_all:
        from ctx.hosts import detect_all

        detected = detect_all(workspace_root=ws.root)
        installed = [d.name for d in detected if d.installed and d.harnessable]
        skipped = [
            d.name for d in detected if d.harnessable and not d.installed
        ]
        if installed:
            try:
                report = setup_hosts(ws, installed)
            except (IntegrationError, OSError) as exc:
                print(f"ctx setup: {exc}", file=sys.stderr)
                return 1
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
        hosts = [name for name in SETUP_HOSTS if name != "hermes"]
        print(
            "no coding-agent CLI detected on PATH; preparing project-local "
            f"integrations ({', '.join(hosts)}) — config is inert until a CLI reads it.\n"
            "Hermes needs its installed CLI; run `ctx setup --host hermes` after installation."
        )

    try:
        print(setup_hosts(ws, hosts))
    except (IntegrationError, OSError) as exc:
        print(f"ctx setup: {exc}", file=sys.stderr)
        return 1
    return 0


def print_config(host: str, ctx_exe: str | None = None) -> str:
    """Copy-pasteable configuration for a host, for CI and docs."""
    from ctx.mcp_hosts import HOSTS, render_config

    if host in HOSTS:
        return render_config(host, Path.cwd(), ctx_exe)
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


def wrap_hermes(workspace_root: Path, agent_args=None) -> int:
    from ctx.mcp_hosts import wrap
    return wrap("hermes", workspace_root, agent_args)


def wrap_omp(workspace_root: Path, agent_args=None) -> int:
    from ctx.mcp_hosts import wrap
    return wrap("omp", workspace_root, agent_args)


def wrap_opencode(workspace_root: Path, agent_args=None) -> int:
    from ctx.mcp_hosts import wrap
    return wrap("opencode", workspace_root, agent_args)


def wrap_dsh(workspace_root: Path, agent_args=None) -> int:
    from ctx.mcp_hosts import wrap
    return wrap("dsh", workspace_root, agent_args)


# ----------------------------------------------------------- prefix parity
# The prefix-budget manifest (ctx.prefixassets) locks the bytes the harness
# injects. It cannot see the bytes the HOST adds because of the harness: a
# wrapper flag that flips a host setting can add tens of KB to every request
# without touching one manifest asset. Measured on DeepSWE (evals/agentbench):
# `--proxy` made Claude Code turn deferred tool loading off, 41 inline tool
# schemas instead of 16, ~15k cached tokens per call, invisible to `ctx gain`.
# The parity probe is the measurement that catches that class: one naive turn,
# one wrapped turn, and the first request's composition diffed against what
# the manifest declares. Two calls to the cheapest model; about one cent.

#: Room for the wrapper's legitimate, undeclared movement in the host prompt:
#: removing native Grep/Glob under collapse (-6 KB), the session-variable
#: parts of the system prompt (cwd, date), and JSON encoding noise.
_PROBE_SLACK_BYTES = 4096
_PROBE_PROMPT = "Reply with the single word: ok"
#: Same threshold the scorecard uses for its `prefix tax` flag: a catalogue
#: this long with no deferral marker is the ~15k-tokens-per-call shape.
_ENVIRONMENT_TAX_MIN_TOOLS = 24


def _prompt_snapshot(config_dir: Path) -> dict | None:
    """Prefix shape of the first request Claude Code sent from a session run
    under ``CLAUDE_CONFIG_DIR=config_dir``: the transcript's prompt_snapshot
    attachment carries the system prompt and the tool list as sent."""
    import glob as _glob

    for path in sorted(_glob.glob(str(config_dir / "projects" / "*" / "*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    att = ev.get("attachment") or {}
                    if att.get("type") != "prompt_snapshot" or "tools" not in att:
                        continue
                    tools = att.get("tools") or []
                    names = sorted(str(t.get("name") or "") for t in tools if isinstance(t, dict))
                    return {
                        "system_bytes": len(str(att.get("systemPrompt", "")).encode("utf-8", "replace")),
                        "tools": len(tools),
                        "tools_bytes": len(json.dumps(tools, ensure_ascii=False).encode("utf-8", "replace")),
                        "deferral": "ToolSearch" in names,
                        "tool_names": names,
                    }
        except OSError:
            continue
    return None


def judge_prefix_parity(naive: dict | None, wrapped: dict | None, declared_bytes: int) -> dict:
    """Pure verdict: the wrapped prefix may exceed the naive one by at most the
    declared resident budget plus slack, and must not lose deferral."""
    if not naive or not wrapped:
        return {"ok": False, "reason": "no prompt snapshot from one of the sessions", "naive": naive, "wrapped": wrapped}
    delta = (wrapped["system_bytes"] + wrapped["tools_bytes"]) - (naive["system_bytes"] + naive["tools_bytes"])
    # Lost deferral is a tax only when there is a catalogue to inline. A
    # wrapped session on the print-mode tool diet has no deferred tools
    # left, so the marker is legitimately absent — and its catalogue is a
    # third of naive's, which the byte delta already credits.
    lost = (
        bool(naive["deferral"]) and not wrapped["deferral"]
        and wrapped["tools"] >= _ENVIRONMENT_TAX_MIN_TOOLS
    )
    allowed = declared_bytes + _PROBE_SLACK_BYTES
    reasons = []
    if lost:
        reasons.append("wrapped session lost tool deferral (the host inlines its whole tool catalogue)")
    if delta > allowed:
        reasons.append(f"wrapped prefix is {delta:,} B over naive; declared budget {declared_bytes:,} B + {_PROBE_SLACK_BYTES:,} B slack")
    # Parity is relative: a tax both sessions pay (ENABLE_TOOL_SEARCH=false in
    # the environment, a host build without deferral) cancels out of the
    # delta. It is still a tax, and the wire audit will flag every session,
    # so name it here rather than let a PASS read as "no tax".
    environment_tax = (
        not naive["deferral"] and not wrapped["deferral"]
        and max(naive["tools"], wrapped["tools"]) >= _ENVIRONMENT_TAX_MIN_TOOLS
    )
    return {
        "ok": not reasons,
        "reason": "; ".join(reasons),
        "delta_bytes": delta,
        "allowed_bytes": allowed,
        "declared_bytes": declared_bytes,
        "deferral_lost": lost,
        "environment_tax": environment_tax,
        "naive": {k: v for k, v in naive.items() if k != "tool_names"},
        "wrapped": {k: v for k, v in wrapped.items() if k != "tool_names"},
        "tools_only_wrapped": sorted(set(wrapped.get("tool_names", [])) - set(naive.get("tool_names", []))),
        "tools_only_naive": sorted(set(naive.get("tool_names", [])) - set(wrapped.get("tool_names", []))),
    }


def render_prefix_parity(verdict: dict) -> str:
    n, w = verdict.get("naive"), verdict.get("wrapped")
    if not n or not w:
        return f"prefix parity: FAIL — {verdict.get('reason')}"
    lines = [
        "prefix parity (first request, bytes as sent):",
        f"  naive   system {n['system_bytes'] / 1024:.1f} KB · tools {n['tools']} ({n['tools_bytes'] / 1024:.0f} KB) · deferral {'on' if n['deferral'] else 'off'}",
        f"  wrapped system {w['system_bytes'] / 1024:.1f} KB · tools {w['tools']} ({w['tools_bytes'] / 1024:.0f} KB) · deferral {'on' if w['deferral'] else 'off'}",
        f"  delta {verdict['delta_bytes']:+,} B · allowed +{verdict['allowed_bytes']:,} B "
        f"(declared {verdict['declared_bytes']:,} B + slack)",
    ]
    if verdict.get("tools_only_wrapped"):
        lines.append("  tools only in wrapped: " + ", ".join(verdict["tools_only_wrapped"][:12]))
    if verdict.get("tools_only_naive"):
        lines.append("  tools only in naive: " + ", ".join(verdict["tools_only_naive"][:12]))
    if verdict.get("environment_tax"):
        lines.append(
            "  ⚠ both sessions inline the whole tool catalogue (deferral off in this "
            "environment — ENABLE_TOOL_SEARCH, or a host build without it); parity "
            "holds, the tax does not cancel"
        )
    lines.append("  verdict: " + ("PASS" if verdict["ok"] else f"FAIL — {verdict['reason']}"))
    return "\n".join(lines)


def probe_prefix(
    workspace_root: Path, ctx_exe: str | None = None, *, model: str = "haiku", use_proxy: bool = True
) -> dict:
    """Run one naive and one wrapped single-turn session and judge parity.

    Each session gets its own ``CLAUDE_CONFIG_DIR`` so neither can see the
    other's transcript; the wrapped one goes through ``wrap_claude`` exactly
    as a real session would (hooks, explorer agent, proxy). Returns the
    verdict dict from :func:`judge_prefix_parity` plus the raw snapshots."""
    from ctx.prefixassets import resident_bytes

    claude = shutil.which("claude")
    if claude is None:
        return {"ok": False, "reason": "`claude` not found on PATH", "naive": None, "wrapped": None}
    exe = ctx_exe or _ctx_executable()
    base_args = ["-p", _PROBE_PROMPT, "--max-turns", "1", "--model", model, "--output-format", "json"]
    with tempfile.TemporaryDirectory(prefix="ctx-probe-") as tmp:
        cfg_naive = Path(tmp) / "naive"
        cfg_wrapped = Path(tmp) / "wrapped"
        cfg_naive.mkdir()
        cfg_wrapped.mkdir()
        subprocess.run(
            [claude, *base_args],
            cwd=workspace_root,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(cfg_naive)},
            capture_output=True, text=True, timeout=300,
        )
        naive = _prompt_snapshot(cfg_naive)
        # wrap_claude builds the child env from os.environ; scope the config
        # dir to this call and restore whatever was there.
        prior = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg_wrapped)
        # wrap_claude inherits stdio so interactive sessions work; the probe's
        # child is not interactive and its JSON result is noise here, so park
        # fd 1 on /dev/null for the duration (stderr keeps the scorecard).
        saved_fd = os.dup(1)
        try:
            with open(os.devnull, "w") as sink:
                sys.stdout.flush()
                os.dup2(sink.fileno(), 1)
                try:
                    wrap_claude(workspace_root, list(base_args), ctx_exe=exe, use_proxy=use_proxy)
                finally:
                    sys.stdout.flush()
                    os.dup2(saved_fd, 1)
        finally:
            os.close(saved_fd)
            if prior is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = prior
        wrapped = _prompt_snapshot(cfg_wrapped)
    declared = sum(resident_bytes().values())
    verdict = judge_prefix_parity(naive, wrapped, declared)
    verdict["model"] = model
    verdict["proxy"] = use_proxy
    return verdict
