"""ctx command-line interface.

Latency design: the hook subcommand (`ctx hook antigravity pre-tool-use`)
is dispatched before argparse and before any heavy import, because it runs
on every intercepted tool call. Everything else loads lazily.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    # ------------------------------------------------------ hook fast path
    if len(args) >= 3 and args[0] == "hook" and args[1] in ("antigravity", "claude-code", "codex"):
        if args[2] == "pre-tool-use":
            from ctx.hook import main_pre_tool_use

            return main_pre_tool_use(flavor=args[1])
        if args[2] == "post-tool-use":
            from ctx.hook import main_post_tool_use

            return main_post_tool_use(flavor=args[1])
        if args[2] == "session-start":
            from ctx.hook import main_session_start

            return main_session_start(flavor=args[1])
        # Unknown hook stage: still emit exactly one valid decision.
        sys.stdout.write('{"decision":"allow"}\n')
        return 0

    # ------------------------------------------------- statusline fast path
    # `ctx statusline <host>` is invoked by a host's status-line command on
    # every render, so it stays off the full-CLI path: read one JSON payload
    # on stdin, print one line, never raise (a status line must not break the
    # host REPL). `ctx statusline codex --rollout <path>` summarises a Codex
    # session rollout instead (Codex's bar is fixed built-in items).
    if args and args[0] == "statusline":
        return _statusline_main(args[1:])

    # ------------------------------------------------- surface gateway fast path
    # `ctx surface gateway` runs the progressive-disclosure MCP server (stdio);
    # it must not parse the full CLI, exactly like `ctx mcp`.
    if len(args) >= 2 and args[0] == "surface" and args[1] == "gateway":
        import os

        from ctx.surface_gateway import serve_gateway

        ws = None
        if "--workspace" in args:
            i = args.index("--workspace")
            ws = args[i + 1] if i + 1 < len(args) else None
        return serve_gateway(ws or os.getcwd())

    # ------------------------------------------------- supervisor fast path
    # Hidden: `ctx job _supervise <jobdir>` is spawned detached by
    # `ctx run --bg`; it must not resolve a workspace or parse the full CLI.
    if len(args) >= 3 and args[0] == "job" and args[1] == "_supervise":
        from ctx.jobs import supervise_main

        return supervise_main(args[2])

    if args and args[0] == "mcp":
        from ctx.mcp import serve

        return serve(bounded_only="--bounded-only" in args)

    return _main_slow(args)


def _statusline_main(rest: list[str]) -> int:
    """Fast path for `ctx statusline <host> [--rollout PATH] [--workspace W]`.
    Fail-open: any error prints nothing and returns 0 so a broken status line
    never surfaces an error into the host's prompt."""
    import json
    import os

    from ctx import statusline

    host = rest[0] if rest else "claude-code"
    rollout = None
    ws = None
    i = 1
    while i < len(rest):
        if rest[i] == "--rollout" and i + 1 < len(rest):
            rollout = rest[i + 1]
            i += 2
        elif rest[i] == "--workspace" and i + 1 < len(rest):
            ws = rest[i + 1]
            i += 2
        else:
            i += 1
    try:
        if rollout is not None:
            line = statusline.codex_rollout_summary(rollout, workspace_root=ws)
        else:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if ws is None:
                ws = (
                    _json_dig(payload, "workspace.current_dir", "workspace.project_dir", "cwd")
                    or os.getcwd()
                )
            # A Codex Stop-hook payload carries no tokens but names the session
            # rollout — summarise that for a priced line.
            tp = _json_dig(payload, "transcript_path")
            if host == "codex" and tp:
                line = statusline.codex_rollout_summary(tp, workspace_root=ws)
            else:
                line = statusline.render(host, payload, workspace_root=ws)
    except Exception:
        line = ""
    if line:
        sys.stdout.write(line + "\n")
    return 0


def _json_dig(obj, *paths):
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur:
            return cur
    return None


# ------------------------------------------------------- command dispatch
# One row per command: the module under ctx.commands that implements it, the
# handler, and whether the dispatcher resolves the workspace for it.
#
# This is a table of strings, not of functions, on purpose. Importing every
# handler at module scope would pull all 34 commands' dependencies into every
# `ctx` invocation — including the hook fast path above, which must stay
# cheap. `_handler_for` imports exactly the one module the invoked command
# needs, which is what the old if/elif chain achieved by inlining.
#
# wants_workspace=False means the handler resolves its own workspace (wrap,
# antigravity) or needs none at all (proxy, replay) — those ran before
# `resolve_workspace` in the chain and still do.
#
# `ctx.cliux` owns the one-line help for each of these names, and
# tests/test_cliux.py holds the two surfaces to each other. Adding a row here
# without a cliux entry (or the reverse) fails that test by design.
_COMMANDS: dict[str, tuple[str, str, bool]] = {
    # module      function            wants_workspace
    "run": ("execute", "cmd_run", True),
    "job": ("execute", "cmd_job", True),
    "jobs": ("execute", "cmd_jobs", True),
    "py": ("execute", "cmd_py", True),
    "seq": ("execute", "cmd_seq", True),
    "search": ("retrieve", "cmd_search", True),
    "get": ("retrieve", "cmd_get", True),
    "stats": ("retrieve", "cmd_stats", True),
    "diff": ("retrieve", "cmd_diff", True),
    "map": ("retrieve", "cmd_map", True),
    "def": ("retrieve", "cmd_def", True),
    "refs": ("retrieve", "cmd_refs", True),
    "diag": ("retrieve", "cmd_diag", True),
    "callers": ("retrieve", "cmd_callers", True),
    "callees": ("retrieve", "cmd_callees", True),
    "impact": ("retrieve", "cmd_impact", True),
    "q": ("retrieve", "cmd_q", True),
    "rewrite": ("rewrite", "cmd_rewrite", True),
    "plan": ("plans", "cmd_plan", True),
    "ask": ("plans", "cmd_ask", True),
    "surface": ("surfaces", "cmd_surface", True),
    "gain": ("admin", "cmd_gain", True),
    "init": ("admin", "cmd_init", True),
    "doctor": ("admin", "cmd_doctor", True),
    "gc": ("admin", "cmd_gc", True),
    "pin": ("admin", "cmd_pin", True),
    "checkpoint": ("admin", "cmd_checkpoint", True),
    "debt": ("admin", "cmd_debt", True),
    "policy": ("admin", "cmd_policy", True),
    "orchestrate": ("hosts", "cmd_orchestrate", True),
    "replay": ("history", "cmd_replay", False),
    "wrap": ("hosts", "cmd_wrap", False),
    "antigravity": ("hosts", "cmd_antigravity", False),
    "proxy": ("hosts", "cmd_proxy", False),
}


# --------------------------------------------------------------- full CLI
def _main_slow(args: list[str]) -> int:
    """The front door, then parse, then one handler. Every command is a row
    in _COMMANDS; nothing is special-cased here."""
    front = _front_door(args)
    if front is not None:
        return front

    parser = _build_parser()
    ns = parser.parse_args(args)

    from ctx.workspace import WorkspaceError, resolve_workspace

    try:
        handler, wants_workspace = _handler_for(ns.cmd)
        if handler is None:
            parser.error(f"unhandled command {ns.cmd!r}")
            return 2  # pragma: no cover
        if not wants_workspace:
            return handler(ns)
        return handler(resolve_workspace(ns.workspace), ns)
    except WorkspaceError as e:
        print(f"ctx: workspace error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ctx: {e}", file=sys.stderr)
        return 1


def _handler_for(cmd: str):
    """Resolve one command name to its implementation, importing exactly the
    one module that implements it (see _COMMANDS). Returns (handler, wants
    workspace); (None, False) for a name the table does not know."""
    entry = _COMMANDS.get(cmd)
    if entry is None:
        return None, False
    module, func, wants_workspace = entry
    from importlib import import_module

    return getattr(import_module("ctx.commands." + module), func), wants_workspace


def _front_door(args: list[str]):
    """The human surface, answered before argparse: bare/`help` invocations
    and misspelled commands. Returns an exit code when it handled the call,
    None to fall through to the parser."""
    from ctx import cliux

    # argparse would print all 34 commands in source order as one wall. Show a
    # grouped, plain-English path instead; `help --all` still reveals everything.
    _flags = {a for a in args if a.startswith("-")}
    _positional = [a for a in args if not a.startswith("-")]
    _wants_all = "--all" in _flags or "--help-all" in _flags
    if not args or args[0] in ("help", "-h", "--help", "--help-all"):
        # `ctx run --help` must still reach argparse; only a bare help asks here.
        if not _positional or _positional[0] == "help":
            print(cliux.render_help(show_all=_wants_all))
            return 0
    # `ctx summarise` should suggest a command, not dump the whole list twice.
    if _positional and _positional[0] not in cliux.all_commands():
        first = _positional[0]
        if not (args and args[0] == "--workspace"):  # --workspace PATH cmd
            print(cliux.did_you_mean(first), file=sys.stderr)
            return 2
    return None


def _build_parser():
    """The whole `ctx` argument surface, built in one place. Argparse is
    imported here, not at module scope, so the fast paths in main() never pay
    for it. Every subcommand's one-liner is overwritten from cliux at the end,
    so the vocabulary a user meets is edited in one file."""
    import argparse

    from ctx import cliux

    class _Parser(argparse.ArgumentParser):
        """argparse's default usage line is the 34-name brace expansion, so a
        single mistyped flag re-floods the wall we just removed. Print the
        compact usage and point at `ctx help` instead."""

        def error(self, message: str):  # noqa: D102
            self.exit(2, f"ctx: {message}\n\nSee:  ctx help\n")

    parser = _Parser(
        prog="ctx",
        usage="ctx [--workspace PATH] <command> [args]",
        description=cliux.TAGLINE,
        epilog=cliux.QUICKSTART,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workspace", metavar="PATH",
        help="repo to work in (default: the git root above the current directory)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="execute a command with birth-time capture")
    p_run.add_argument("--focus", help="deterministic evidence-selection query")
    p_run.add_argument("--cwd", help="working directory relative to the workspace")
    p_run.add_argument("--shell", action="store_true", help="run one string through the shell")
    p_run.add_argument("--timeout", type=float, default=600.0)
    p_run.add_argument(
        "--bg", action="store_true",
        help="background immediately: supervised run, transcript gets a job handle",
    )
    p_run.add_argument(
        "--bg-after", type=float, dest="bg_after", metavar="T",
        help="stay foreground up to T seconds; if still running, background",
    )
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> [args...]")

    p_search = sub.add_parser("search", help="multi-pattern bounded search")
    p_search.add_argument("ref", metavar="handle",
                          help="what to search: repo:path, or a run:/blob: handle")
    p_search.add_argument("patterns", nargs="+")
    p_search.add_argument("--fixed", action="store_true", help="fixed strings, not regex")
    p_search.add_argument("--all", action="store_true", help="require all patterns per target")
    p_search.add_argument("--context", type=int, default=0)
    p_search.add_argument("--glob", help="path glob for repo: searches")
    p_search.add_argument("--scope", help="named monorepo scope from ctx.toml")
    p_search.add_argument("--max-matches", type=int, dest="max_matches")

    p_get = sub.add_parser("get", help="exact bounded slice of a file or artifact")
    p_get.add_argument("ref", metavar="handle",
                       help="what to read: repo:path, or a run:/blob: handle")
    p_get.add_argument("--lines", help="A:B line span")
    p_get.add_argument("--bytes", help="A:B byte span")
    p_get.add_argument("--records", help="A:B record span (JSONL)")
    p_get.add_argument("--json-pointer", dest="json_pointer", help="RFC 6901 pointer")
    p_get.add_argument("--symbol", help="Python def/class dotted name (stdlib ast)")
    p_get.add_argument("--span", help="opaque span token minted by a digest")

    p_diff = sub.add_parser("diff", help="run-to-run regression delta digest")
    p_diff.add_argument("ref_a", metavar="handle_before", help="the earlier run handle")
    p_diff.add_argument("ref_b", metavar="handle_after", help="the later run handle")

    p_rewrite = sub.add_parser(
        "rewrite", help="structural multi-file rewrite in one op (find+edit, transactional)")
    p_rewrite.add_argument("pattern", help="ast-grep metavariable pattern, e.g. 'foo($A)'")
    p_rewrite.add_argument("replacement", help="rewrite template, e.g. 'bar($A)'")
    p_rewrite.add_argument("--lang", help="language: py|js|ts|go|rust|…")
    p_rewrite.add_argument("--glob", help="path glob to scope files")
    p_rewrite.add_argument("--apply", action="store_true",
                           help="write the change (default: preview the diff only)")

    p_stats = sub.add_parser("stats", help="bounded schema/shape statistics")
    p_stats.add_argument("ref", nargs="?", default="repo:", metavar="handle",
                         help="what to describe (default: this repo)")
    p_stats.add_argument("--scope", help="named monorepo scope")
    p_stats.add_argument(
        "--session",
        action="store_true",
        help="render the current session's wire scorecard (proxy required)",
    )

    p_py = sub.add_parser(
        "py", help="run a Python script; only its digest returns"
    )
    p_py.add_argument(
        "script", nargs="?", help="script text ('-' or omitted reads stdin/heredoc)"
    )
    p_py.add_argument("--file", help="read the script from a workspace file")
    p_py.add_argument("--cwd", help="working directory relative to the workspace")
    p_py.add_argument("--timeout", type=float, default=600.0)
    p_py.add_argument("--focus", help="deterministic evidence-selection query")

    p_job = sub.add_parser("job", help="inspect or control a backgrounded run")
    p_job.add_argument("job_id", help="job id from `ctx run --bg` (prefix ok)")
    p_job.add_argument("--tail", type=int, metavar="N", help="show last N spool lines")
    p_job.add_argument("--wait", action="store_true", help="block until done, then digest")
    p_job.add_argument("--timeout", type=float, help="give up --wait after T seconds")
    p_job.add_argument(
        "--kill", action="store_true",
        help="SIGKILL the process group; finalize what spooled",
    )

    sub.add_parser("jobs", help="list this workspace's backgrounded runs")

    p_seq = sub.add_parser("seq", help="declared command tree: N steps, one round")
    p_seq.add_argument("steps", nargs="+", help="shell command strings, run in order")
    p_seq.add_argument("--keep-going", action="store_true", dest="keep_going",
                       help="run remaining steps after a failure (default: halt)")
    p_seq.add_argument("--timeout", type=float, help="per-step timeout seconds")
    p_seq.add_argument("--focus", help="bias step digests toward this question")

    sub.add_parser("gain", help="cumulative token/cost savings from telemetry")

    p_surface = sub.add_parser(
        "surface",
        help="audit the capability surface (input side of containment): "
             "inventory · audit · explain · trim",
    )
    p_surface.add_argument(
        "surface_cmd",
        choices=("inventory", "audit", "explain", "trim", "graph", "compile",
                 "reconcile", "referee", "install-gateway"),
        help="inventory · audit · explain · trim · graph · compile · reconcile · "
             "referee · install-gateway",
    )
    p_surface.add_argument("target", nargs="?", help="capability id for `explain`")
    p_surface.add_argument("--json", action="store_true", help="emit structured JSON")
    p_surface.add_argument(
        "--probe-mcp", action="store_true",
        help="spawn each MCP server to measure its real per-tool schema tokens",
    )
    p_surface.add_argument("--profile", help="profile name for `compile` (read-only|local-dev|review|full or ctx.toml)")
    p_surface.add_argument("--host", default="claude", choices=("claude", "codex", "antigravity"),
                           help="target host for `compile` (default: claude)")
    p_surface.add_argument("--apply", action="store_true",
                           help="`compile`: write the minimal config under .ctx-surface/")
    p_surface.add_argument("--intent", default="",
                           help="`reconcile`: task-intent text used to trigger reveals")
    p_surface.add_argument("--phase", help="`reconcile`: override inferred phase (explore|edit|verify|deliver)")
    p_surface.add_argument("--enforce", action="store_true",
                           help="`reconcile`: apply actions to gateway state (else shadow only)")

    p_replay = sub.add_parser(
        "replay",
        help="deterministic open-loop replay of recorded Claude Code transcripts",
    )
    p_replay.add_argument("transcripts", nargs="*", help="transcript .jsonl paths")
    p_replay.add_argument(
        "--all-projects",
        action="store_true",
        help="replay every session under ~/.claude/projects",
    )
    p_replay.add_argument("--gaps", action="store_true", help="aggregate coverage-gap table")
    p_replay.add_argument(
        "--regret", dest="replay_regret", action="store_true",
        help="evidence-regret scoreboard: R = actual − oracle per profile "
        "(the measured rate–distortion frontier gap, docs/THEORY.md)",
    )
    p_replay.add_argument(
        "--outcomes", dest="replay_outcomes", action="store_true",
        help="evidence_followup/v1 scoreboard: which emissions were observably "
        "followed up (association, not causation; deterministic, read-only; "
        "open windows censored)",
    )
    p_replay.add_argument(
        "--append-ledger", dest="replay_append_ledger", action="store_true",
        help="with --outcomes: append the events to the workspace "
        ".ctx-session-reads/evidence-followups.jsonl ledger (the input to "
        "`ctx policy compile --plan-value`) — an explicit user action, "
        "never a runtime side effect",
    )
    p_replay.add_argument("--json", dest="replay_json", action="store_true")

    p_debt = sub.add_parser("debt", help="declared-omission ledger for deferred decisions")
    debt_sub = p_debt.add_subparsers(dest="debt_cmd", required=True)
    p_da = debt_sub.add_parser("add", help="declare a deferred decision")
    p_da.add_argument("note", help="what was deferred and why")
    p_da.add_argument("--ref", default="", help="coordinates, e.g. repo:src/x.py:120")
    debt_sub.add_parser("list", help="show outstanding declared debt")
    p_dr = debt_sub.add_parser("resolve", help="mark a debt entry resolved")
    p_dr.add_argument("id", help="entry id from `ctx debt list`")

    p_map = sub.add_parser("map", help="ranked, budget-fitted codebase map")
    p_map.add_argument("--budget", type=int, default=600, help="token budget")
    p_map.add_argument("--focus", help="boost files whose path or symbols match")

    p_def = sub.add_parser("def", help="symbol definition site (snapshot + span)")
    p_def.add_argument("target", help="repo:<path>:<Symbol.dotted>")

    p_refs = sub.add_parser("refs", help="reference sites for a symbol")
    p_refs.add_argument("symbol", help="name or Class.method dotted name")
    p_refs.add_argument("--path", help="restrict sites to a subtree")

    p_diag = sub.add_parser("diag", help="deterministic lint/syntax digest")
    p_diag.add_argument("path", nargs="?", help="restrict to a subtree")

    p_callers = sub.add_parser("callers", help="who calls this symbol (call graph)")
    p_callers.add_argument("symbol", help="name or Class.method dotted name")
    p_callees = sub.add_parser("callees", help="what this symbol calls (call graph)")
    p_callees.add_argument("symbol", help="name or Class.method dotted name")
    p_impact = sub.add_parser("impact", help="transitive callers / blast radius")
    p_impact.add_argument("symbol", help="name or Class.method dotted name")
    p_impact.add_argument("--depth", type=int, default=6, help="max hops (≤6)")

    p_q = sub.add_parser(
        "q", help="total pipeline algebra: '<stage> | <stage> | …' over typed streams"
    )
    p_q.add_argument(
        "query",
        help="e.g. 'refs TokenBucket | group file | top 3 | get --context 5'",
    )
    p_q.add_argument(
        "--trace", action="store_true", help="append per-stage row provenance"
    )

    p_plan = sub.add_parser(
        "plan", help="compiled evidence plans: validate | price | run | ops"
    )
    plan_sub = p_plan.add_subparsers(dest="plan_cmd", required=True)
    for _pc, _help in (
        ("validate", "static totality/capability check; typed rejections"),
        ("price", "pre-execution cost card (nodes, units, wall budget)"),
        ("run", "execute the plan DAG; one investigation digest returns"),
    ):
        _pp = plan_sub.add_parser(_pc, help=_help)
        _pp.add_argument(
            "plan_file", nargs="?", default="-",
            help="plan JSON path ('-' or omitted reads stdin)",
        )
        if _pc == "run":
            # Absorbed from the old top-level `investigate`, which was this same
            # execution plus a replan ledger — a flag, never a second command.
            _pp.add_argument(
                "--replans", type=int, default=None,
                help="how many replans this question may take "
                "(default from ctx.toml [plan])",
            )
            _pp.add_argument(
                "--advise", dest="inv_advise", action="store_true",
                help="afterwards, report which operator order the recorded "
                "history would have preferred (report only; nothing is changed)",
            )
        if _pc == "price":
            _pp.add_argument(
                "--value", dest="plan_value_explain", action="store_true",
                help="also show the shadow follow-up ranking of the plan's ops "
                "(Wilson lower bounds over compiled counts; report only)",
            )
    plan_sub.add_parser("ops", help="registered logical operators (the plan author's inventory)")

    p_ask = sub.add_parser(
        "ask",
        help="answer a repository question via a typed intent preset "
        "(locate|impact|diagnose|trace|compare|verify|review) — one evidence plan",
    )
    p_ask.add_argument("question", help="the repository question (plain text)")
    p_ask.add_argument(
        "--intent", dest="ask_intent", default=None,
        help="locate|impact|diagnose|trace|compare|verify|review (required "
        "unless unambiguous; the error suggests one — it never guesses and runs)",
    )
    p_ask.add_argument(
        "--symbol", dest="ask_symbol", default=None,
        help="the subject symbol (overrides inference from the question)",
    )
    p_ask.add_argument(
        "--run", dest="ask_run", default=None,
        help="run handle: the failure run for diagnose; run A for compare "
        "(default: the latest captured run)",
    )
    p_ask.add_argument(
        "--against", dest="ask_against", default=None,
        help="compare: run B (the second run handle to diff --run against)",
    )
    p_ask.add_argument(
        "--command", dest="ask_command", default=None,
        help="verify/review: the test command to run (default: python -m pytest -q)",
    )
    p_ask.add_argument("--depth", dest="ask_depth", type=int, default=None,
                       help="impact/trace blast-radius depth (≤6)")
    p_ask.add_argument(
        "--plan", dest="ask_show_plan", action="store_true",
        help="print the compiled ctx.plan/v1 and disclosure, do not execute",
    )
    p_ask.add_argument(
        "--replans", type=int, default=None,
        help="epoch allowance for this objective (default from ctx.toml [plan])",
    )

    p_policy = sub.add_parser("policy", help="compiled steering policy")
    pol_sub = p_policy.add_subparsers(dest="policy_cmd", required=True)
    p_pc = pol_sub.add_parser("compile", help="compile policy from telemetry")
    p_pc.add_argument("--min-runs", type=int, dest="min_runs")
    p_pc.add_argument(
        "--plan-value", dest="plan_value", action="store_true",
        help="also compile [plan_value] priors from the evidence-outcome "
        "ledger (advisory investigation-ranking input; deterministic, "
        "reviewable, never written by runtime)",
    )
    pol_sub.add_parser("show", help="print the compiled policy")

    sub.add_parser("init", help="write ctx.toml and .ctxignore templates")

    p_doctor = sub.add_parser("doctor", help="validate installation and store health")
    p_doctor.add_argument("--antigravity", action="store_true")

    p_gc = sub.add_parser("gc", help="mark-and-sweep expired artifacts")
    p_gc.add_argument("--retention-days", type=int, dest="retention_days")

    p_pin = sub.add_parser("pin", help="pin an artifact against garbage collection")
    p_pin.add_argument("ref", metavar="handle", help="the artifact handle to keep")

    p_cp = sub.add_parser("checkpoint", help="freeze task state into a new cache epoch")
    p_cp.add_argument("--goal", help="task goal (required to create)")
    p_cp.add_argument("--state", help="current state summary")
    p_cp.add_argument("--decision", action="append", default=[], dest="decisions")
    p_cp.add_argument("--hypothesis", action="append", default=[], dest="hypotheses")
    p_cp.add_argument(
        "--evidence", action="append", default=[],
        help="'<ref> [note]' — handle plus optional coordinates/note; pinned",
    )
    p_cp.add_argument("--attempted", action="append", default=[])
    p_cp.add_argument("--file", action="append", default=[], dest="files")
    p_cp.add_argument("--show", help="render an existing checkpoint:<id>")

    p_proxy = sub.add_parser(
        "proxy", help="pass-through observer proxy for Anthropic API traffic"
    )
    p_proxy.add_argument("--port", type=int, required=True)
    p_proxy.add_argument("--upstream", required=True, help="e.g. https://api.anthropic.com")
    p_proxy.add_argument("--state-dir", required=True, dest="state_dir")
    p_proxy.add_argument("--workspace-id", default="", dest="workspace_id")
    p_proxy.add_argument(
        "--rescue-pct", type=float, default=0.0, dest="rescue_pct",
        help="opt-in Tier-1 lossless rescue: at this window %%, elide old "
        "large tool_results to file-backed stubs (0 = pure observer)",
    )

    p_wrap = sub.add_parser(
        "wrap",
        help="set up / run the harness for a host (built for Antigravity, "
        "works with Claude Code and Codex)",
    )
    p_wrap.add_argument(
        "host",
        choices=["setup", "all", "detect", "claude", "antigravity", "codex"],
        help="'setup' detects & harnesses installed CLIs; 'all' forces every "
        "supported host; 'detect' lists installed CLIs priced by model",
    )
    p_wrap.add_argument(
        "--print-config", action="store_true", dest="print_config",
        help="print the host configuration instead of launching",
    )
    p_wrap.add_argument("agent_args", nargs=argparse.REMAINDER, help="-- <agent args...>")

    p_orch = sub.add_parser(
        "orchestrate",
        help="route a task's phases across installed harnesses by model cost "
        "(harness collaboration); prices the plan, then runs it",
    )
    p_orch.add_argument("task", help="the task to collaborate on")
    p_orch.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="print the priced routing plan and stop (launch no harness)",
    )
    p_orch.add_argument(
        "--run", action="store_true", dest="force_run",
        help="execute even when [orchestrate] confirm=true",
    )

    p_agy = sub.add_parser("antigravity", help="Antigravity integration")
    agy_sub = p_agy.add_subparsers(dest="agy_cmd", required=True)
    p_install = agy_sub.add_parser("install", help="render the repo-scoped plugin")
    p_install.add_argument("--scope", default="workspace", choices=["workspace"])
    p_install.add_argument("--workspace", dest="agy_workspace", default=".")

    # Every subcommand's one-liner comes from cliux, so the vocabulary a user
    # meets is edited in one place instead of 34 scattered `help=` strings.
    # Display-only; guarded so a future argparse internal can never break the CLI.
    try:
        for _action in sub._choices_actions:  # noqa: SLF001
            _line = cliux.help_line(_action.dest)
            if _line:
                _action.help = _line
    except Exception:  # pragma: no cover - cosmetic only
        pass

    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
