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


# --------------------------------------------------------------- full CLI
def _main_slow(args: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ctx",
        description=(
            "straitjacket context harness: unbounded output becomes an immutable "
            "artifact plus a bounded deterministic digest."
        ),
    )
    parser.add_argument("--workspace", help="explicit workspace path")
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
    p_search.add_argument("ref")
    p_search.add_argument("patterns", nargs="+")
    p_search.add_argument("--fixed", action="store_true", help="fixed strings, not regex")
    p_search.add_argument("--all", action="store_true", help="require all patterns per target")
    p_search.add_argument("--context", type=int, default=0)
    p_search.add_argument("--glob", help="path glob for repo: searches")
    p_search.add_argument("--scope", help="named monorepo scope from ctx.toml")
    p_search.add_argument("--max-matches", type=int, dest="max_matches")

    p_get = sub.add_parser("get", help="exact bounded slice of a file or artifact")
    p_get.add_argument("ref")
    p_get.add_argument("--lines", help="A:B line span")
    p_get.add_argument("--bytes", help="A:B byte span")
    p_get.add_argument("--records", help="A:B record span (JSONL)")
    p_get.add_argument("--json-pointer", dest="json_pointer", help="RFC 6901 pointer")
    p_get.add_argument("--symbol", help="Python def/class dotted name (stdlib ast)")
    p_get.add_argument("--span", help="opaque span token minted by a digest")

    p_diff = sub.add_parser("diff", help="run-to-run regression delta digest")
    p_diff.add_argument("ref_a", help="baseline run: reference")
    p_diff.add_argument("ref_b", help="comparison run: reference")

    p_stats = sub.add_parser("stats", help="bounded schema/shape statistics")
    p_stats.add_argument("ref", nargs="?", default="repo:")
    p_stats.add_argument("--scope", help="named monorepo scope")
    p_stats.add_argument(
        "--session",
        action="store_true",
        help="render the current session's wire scorecard (proxy required)",
    )

    p_eval = sub.add_parser(
        "eval", help="run a Python script under the birth gate; only its digest returns"
    )
    p_eval.add_argument(
        "script", nargs="?", help="script text ('-' or omitted reads stdin/heredoc)"
    )
    p_eval.add_argument("--file", help="read the script from a workspace file")
    p_eval.add_argument("--cwd", help="working directory relative to the workspace")
    p_eval.add_argument("--timeout", type=float, default=600.0)
    p_eval.add_argument("--focus", help="deterministic evidence-selection query")

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
        if _pc == "price":
            _pp.add_argument(
                "--value", dest="plan_value_explain", action="store_true",
                help="also show the shadow follow-up ranking of the plan's ops "
                "(Wilson lower bounds over compiled counts; report only)",
            )
    plan_sub.add_parser("ops", help="registered logical operators (the plan author's inventory)")

    p_inv = sub.add_parser(
        "investigate",
        help="one hypothesis epoch: execute a compiled evidence plan, get one digest",
    )
    p_inv.add_argument(
        "plan_file", nargs="?", default="-",
        help="plan JSON path ('-' or omitted reads stdin)",
    )
    p_inv.add_argument(
        "--replans", type=int, default=None,
        help="epoch allowance for this objective (default from ctx.toml [plan])",
    )
    p_inv.add_argument(
        "--advise", dest="inv_advise", action="store_true",
        help="after execution: shadow follow-up report — declared vs "
        "empirically-preferred operator ordering with the lexicographic "
        "reason (report only: nothing is reordered or suppressed)",
    )

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
    p_pin.add_argument("ref")

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
        choices=["setup", "all", "claude", "antigravity", "codex"],
        help="'setup' (or 'all') harnesses every host in one command",
    )
    p_wrap.add_argument(
        "--print-config", action="store_true", dest="print_config",
        help="print the host configuration instead of launching",
    )
    p_wrap.add_argument("agent_args", nargs=argparse.REMAINDER, help="-- <agent args...>")

    p_agy = sub.add_parser("antigravity", help="Antigravity integration")
    agy_sub = p_agy.add_subparsers(dest="agy_cmd", required=True)
    p_install = agy_sub.add_parser("install", help="render the repo-scoped plugin")
    p_install.add_argument("--scope", default="workspace", choices=["workspace"])
    p_install.add_argument("--workspace", dest="agy_workspace", default=".")

    ns = parser.parse_args(args)

    from ctx.workspace import WorkspaceError, resolve_workspace

    try:
        if ns.cmd == "replay":
            # Workspace-free by design: history replay must run on any
            # machine that has ~/.claude/projects, harnessed or not.
            import json as _json

            from ctx.replay import (
                default_history_paths,
                render_regret,
                render_report,
                simulate_session,
            )

            paths = list(ns.transcripts)
            if ns.all_projects:
                paths += default_history_paths()
            if not paths:
                print("no transcripts given (pass paths or --all-projects)")
                return 1
            if ns.replay_outcomes:
                from ctx.replay import render_outcomes, session_outcomes

                events = [e for p in paths for e in session_outcomes(p)]
                if ns.replay_append_ledger:
                    from ctx.workspace import resolve_workspace as _rw

                    _ws = _rw(ns.workspace)
                    ldir = _ws.root / ".ctx-session-reads"
                    ldir.mkdir(parents=True, exist_ok=True)
                    with (ldir / "evidence-followups.jsonl").open(
                        "a", encoding="utf-8"
                    ) as fh:
                        for e in events:
                            fh.write(_json.dumps(e.payload(), sort_keys=True) + "\n")
                    print(f"appended {len(events)} events to {ldir / 'evidence-outcomes.jsonl'}")
                if ns.replay_json:
                    print(_json.dumps([e.payload() for e in events], indent=2))
                else:
                    print(render_outcomes(events))
                return 0
            reports = [simulate_session(p) for p in paths]
            if ns.replay_json:
                print(_json.dumps(reports, indent=2))
            elif ns.replay_regret:
                print(render_regret(reports))
            else:
                print(render_report(reports, gaps=ns.gaps))
            return 0

        if ns.cmd == "antigravity" and ns.agy_cmd == "install":
            ws = resolve_workspace(ns.workspace or ns.agy_workspace)
            from ctx.installer import install_antigravity

            print(install_antigravity(ws))
            return 0

        if ns.cmd == "proxy":
            from pathlib import Path as _Path

            from ctx.proxy import serve_proxy

            serve_proxy(
                ns.port,
                ns.upstream,
                _Path(ns.state_dir),
                ns.workspace_id,
                rescue_pct=ns.rescue_pct,
            )
            return 0

        if ns.cmd == "wrap":
            from ctx.wrap import (
                print_config,
                wrap_antigravity,
                wrap_claude,
                wrap_codex,
                wrap_setup,
            )

            agent_args = list(ns.agent_args)
            # REMAINDER swallows options placed after the host positional;
            # recognize --print-config there too (but never past the `--`).
            if "--print-config" in agent_args:
                tail = agent_args.index("--") if "--" in agent_args else len(agent_args)
                if agent_args.index("--print-config") < tail:
                    ns.print_config = True
                    agent_args.remove("--print-config")
            use_proxy = False
            if "--proxy" in agent_args:
                tail = agent_args.index("--") if "--" in agent_args else len(agent_args)
                if agent_args.index("--proxy") < tail:
                    use_proxy = True
                    agent_args.remove("--proxy")
            use_gateway = False
            if "--gateway" in agent_args:
                tail = agent_args.index("--") if "--" in agent_args else len(agent_args)
                if agent_args.index("--gateway") < tail:
                    use_gateway = True
                    agent_args.remove("--gateway")
            rescue_pct = 0.0
            if "--rescue-pct" in agent_args:
                tail = agent_args.index("--") if "--" in agent_args else len(agent_args)
                i = agent_args.index("--rescue-pct")
                if i < tail and i + 1 < len(agent_args):
                    try:
                        rescue_pct = float(agent_args[i + 1])
                    except ValueError:
                        rescue_pct = 0.0
                    del agent_args[i : i + 2]
                    use_proxy = True  # rescue implies the proxy
            if ns.print_config:
                host = "claude" if ns.host in ("setup", "all") else ns.host
                print(print_config(host))
                return 0
            ws = resolve_workspace(ns.workspace)
            if agent_args and agent_args[0] == "--":
                agent_args = agent_args[1:]
            # --gateway: set up the host(s) AND wire the progressive-disclosure
            # gateway, so unrevealed MCP tool schemas never enter context.
            if use_gateway:
                from ctx.installer import install_claude, install_gateway

                hosts = (("claude", "codex", "antigravity")
                         if ns.host in ("setup", "all") else (ns.host,))
                if ns.host in ("setup", "all"):
                    wrap_setup(ws.root)
                elif ns.host == "codex":
                    wrap_codex(ws.root)
                elif ns.host == "antigravity":
                    wrap_antigravity(ws.root)
                elif ns.host == "claude":
                    print(install_claude(resolve_workspace(str(ws.root))))
                print()
                for h in hosts:
                    print(install_gateway(resolve_workspace(str(ws.root)), h, apply=True))
                    print()
                return 0
            # Single-command multi-host setup (built for Antigravity; also
            # harnesses Claude Code and Codex).
            if ns.host in ("setup", "all"):
                return wrap_setup(ws.root)
            if ns.host == "codex":
                return wrap_codex(ws.root)
            if ns.host == "antigravity":
                return wrap_antigravity(ws.root)
            # claude: launch ephemerally when given agent args, else persist.
            if ns.host == "claude":
                if agent_args:
                    return wrap_claude(
                        ws.root, agent_args, use_proxy=use_proxy, rescue_pct=rescue_pct
                    )
                from ctx.installer import install_claude

                print(install_claude(resolve_workspace(str(ws.root))))
                print()
                print("Claude Code sessions in this workspace are now harnessed. "
                      "For an ephemeral, zero-residue run instead: "
                      "ctx wrap claude -- -p \"...\"")
                return 0
            return wrap_antigravity(ws.root)

        ws = resolve_workspace(ns.workspace)

        if ns.cmd == "run":
            return _cmd_run(ws, ns)
        if ns.cmd == "job":
            return _cmd_job(ws, ns)
        if ns.cmd == "jobs":
            return _cmd_jobs(ws)
        if ns.cmd == "eval":
            return _cmd_eval(ws, ns)
        if ns.cmd == "search":
            return _cmd_retrieval(ws, ns, "search")
        if ns.cmd == "get":
            return _cmd_retrieval(ws, ns, "get")
        if ns.cmd == "stats":
            if getattr(ns, "session", False):
                from ctx.scorecard import compute_scorecard, render_scorecard

                sc = compute_scorecard(ws.root / ".ctx-session-reads" / "proxy")
                if sc is None:
                    print(
                        "no wire observations for this workspace "
                        "(run under `ctx wrap claude --proxy`)"
                    )
                    return 1
                print(render_scorecard(sc))
                return 0
            return _cmd_retrieval(ws, ns, "stats")
        if ns.cmd == "seq":
            from ctx.seq import run_seq
            from ctx.store import Store as _Store

            store = _Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            text, code = run_seq(
                ws, store, ns.steps,
                halt_on_fail=not ns.keep_going,
                timeout=ns.timeout, focus=ns.focus,
            )
            # Delivery plan (EDC §13): seq always emits against the result
            # budget; failure asymmetry + pressure compose in the resolver.
            # Engagement parity with run/eval (docs/LADDERS.md edge 1): lean
            # or passive sessions must not pay for suggestion lines here
            # either — _emit_bounded_digest applies the same filter.
            plan = _delivery_plan(
                ws,
                outcome="success" if code == 0 else "failure",
                family="seq",
                base_tokens=ws.config.budgets.result_tokens,
            )
            _emit_bounded_digest(ws, store, text, plan)
            return 0 if code == 0 else 3
        if ns.cmd == "gain":
            return _cmd_gain(ws)
        if ns.cmd == "surface":
            return _cmd_surface(ws, ns)
        if ns.cmd == "debt":
            from ctx import debt as _debt

            if ns.debt_cmd == "add":
                eid = _debt.add(ws.root, ns.note, ref=ns.ref)
                print(f"declared: {eid}")
                return 0
            if ns.debt_cmd == "resolve":
                ok = _debt.resolve(ws.root, ns.id)
                print("resolved" if ok else f"unknown debt id: {ns.id}")
                return 0 if ok else 1
            print(_debt.render(ws.root))
            return 0
        if ns.cmd == "diff":
            return _cmd_diff(ws, ns)
        if ns.cmd == "map":
            return _cmd_map(ws, ns)
        if ns.cmd in ("def", "refs", "diag"):
            return _cmd_code(ws, ns)
        if ns.cmd in ("callers", "callees", "impact"):
            from ctx.callgraph import cmd_callees, cmd_callers, cmd_impact
            from ctx.store import Store as _S

            store = _S(ws.workspace_id, retention_days=ws.config.store.retention_days)
            if ns.cmd == "callers":
                print(cmd_callers(store, ws, ns.symbol))
            elif ns.cmd == "callees":
                print(cmd_callees(store, ws, ns.symbol))
            else:
                print(cmd_impact(store, ws, ns.symbol, depth=ns.depth))
            return 0
        if ns.cmd == "q":
            return _cmd_q(ws, ns)
        if ns.cmd == "plan":
            return _cmd_plan(ws, ns)
        if ns.cmd == "investigate":
            return _cmd_investigate(ws, ns)
        if ns.cmd == "ask":
            return _cmd_ask(ws, ns)
        if ns.cmd == "policy":
            return _cmd_policy(ws, ns)
        if ns.cmd == "init":
            from ctx.installer import init_workspace

            print("\n".join(init_workspace(ws.root)) or "nothing to do")
            return 0
        if ns.cmd == "doctor":
            from ctx.installer import doctor_report

            report = doctor_report(ws, antigravity=ns.antigravity)
            print(report)
            return 0 if "PROBLEMS" not in report.splitlines()[0] else 1
        if ns.cmd == "gc":
            from ctx.store import Store

            store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            days = ns.retention_days or ws.config.store.retention_days
            result = store.gc(days)
            print(
                f"gc: removed {result['blobs_removed']} blobs, "
                f"{result['manifests_removed']} manifests (retention {days}d)"
            )
            return 0
        if ns.cmd == "pin":
            from ctx.refs import parse_ref
            from ctx.store import Store

            ref = parse_ref(ns.ref)
            store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            store.pin(ref.id or "")
            print(f"pinned {ref.display()}")
            return 0
        if ns.cmd == "checkpoint":
            from ctx.checkpoint import create_checkpoint, show_checkpoint
            from ctx.store import Store

            store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            if ns.show:
                print(show_checkpoint(store, ws, ns.show))
                return 0
            if not ns.goal:
                print("ctx checkpoint: --goal is required (or --show <checkpoint:id>)", file=sys.stderr)
                return 2
            _, doc = create_checkpoint(
                store,
                ws,
                goal=ns.goal,
                state=ns.state,
                decisions=ns.decisions,
                hypotheses=ns.hypotheses,
                evidence=ns.evidence,
                attempted=ns.attempted,
                files=ns.files,
            )
            print(doc)
            return 0
    except WorkspaceError as e:
        print(f"ctx: workspace error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ctx: {e}", file=sys.stderr)
        return 1

    parser.error(f"unhandled command {ns.cmd!r}")
    return 2  # pragma: no cover


def _cmd_surface(ws, ns) -> int:
    """`ctx surface {inventory,audit,explain,trim}` — the input side of
    containment: measure the discretionary capability surface, never mutate it
    (Phase 1). All rendering is bounded and deterministic."""
    import json as _json

    from ctx import surface

    if ns.surface_cmd == "install-gateway":
        from ctx.installer import install_gateway

        print(install_gateway(ws, ns.host, apply=ns.apply))
        return 0

    if ns.surface_cmd in ("reconcile", "referee"):
        from ctx import surface_reconcile as sr

        if ns.surface_cmd == "referee":
            rep = sr.referee(ws.root)
            print(_json.dumps(rep, indent=2) if ns.json else
                  f"[ctx surface referee] hides scored: {rep['hides_scored']} · "
                  f"safe {rep['safe']} · unsafe {rep['unsafe']} · verdict {rep['verdict']}"
                  + ("\n  promotable: " + ", ".join(rep["promotable"]) if rep["promotable"] else ""))
            return 0
        rep = sr.run_reconcile(ws.root, intent=ns.intent, phase=ns.phase, enforce=ns.enforce)
        print(_json.dumps(rep, indent=2) if ns.json else sr.render_reconcile(rep))
        return 0

    if ns.surface_cmd == "compile":
        from ctx import surface_profiles

        if not ns.profile:
            print("usage: ctx surface compile --profile <name> [--host HOST] [--apply]")
            print("built-in profiles: " + ", ".join(surface_profiles.BUILTIN_PROFILES))
            return 2
        rep = surface_profiles.compile_profile(
            ws.root, ns.profile, host=ns.host, apply=ns.apply,
            probe_mcp=getattr(ns, "probe_mcp", False))
        if ns.json:
            print(_json.dumps(rep, indent=2))
            return 0
        print(surface_profiles.render_compile(rep))
        return 1 if rep.get("error") else 0

    if ns.surface_cmd == "explain":
        if not ns.target:
            print("usage: ctx surface explain <capability-id>")
            return 2
        records = surface.detect_overlaps(surface.collect_surface(ws.root))
        counts = surface.observed_tool_counts(ws.root)
        records = [surface._with(c, invocations=surface._match_invocations(c, counts))
                   for c in records]
        match = next((c for c in records if c.id == ns.target), None)
        if match is None:
            print(f"no capability {ns.target!r}; run `ctx surface inventory`")
            return 1
        print(surface.render_explain(match))
        return 0

    if ns.surface_cmd == "inventory":
        base = surface.collect_surface(ws.root)
        if getattr(ns, "probe_mcp", False):
            probed = surface.probe_surface(ws.root)
            provs = {p.provider for p in probed}
            base = [c for c in base
                    if not (c.kind == "mcp_server" and c.provider in provs)] + probed
        records = surface.detect_overlaps(base)
        counts = surface.observed_tool_counts(ws.root)
        records = [surface._with(c, invocations=surface._match_invocations(c, counts))
                   for c in records]
        if ns.json:
            print(_json.dumps([c.as_dict() for c in records], indent=2))
        else:
            print(surface.render_inventory(records))
        return 0

    # audit / trim / graph build the full audit.
    a = surface.audit(ws.root, probe_mcp=getattr(ns, "probe_mcp", False))
    if ns.json:
        print(_json.dumps(a, indent=2))
        return 0
    if ns.surface_cmd == "graph":
        recs = [surface.Capability(**{k: (tuple(v) if isinstance(v, list) else v)
                                      for k, v in r.items() if k != "recommended_level"})
                for r in a["records"]]
        print(surface.render_graph(recs, a["graph"]))
        return 0
    if ns.surface_cmd == "trim":
        tp = a["trim_preview"]
        print("[ctx surface trim --preview · advisory only, nothing hidden]")
        if not tp["ids"]:
            print("  nothing to defer: surface is already lean")
            return 0
        for cid in tp["ids"]:
            rec = next(c for c in a["records"] if c["id"] == cid)
            print(f"  defer  {rec['tokens']:>6,} tok  {cid:<28} "
                  f"auth={rec['authority']} → {rec['recommended_level']}")
        print(f"  ── est {tp['est_token_reduction']:,} tokens/turn recoverable")
        return 0
    print(surface.render_audit(a))
    return 0


def _cmd_gain(ws) -> int:
    """Cumulative containment savings, made legible (rtk's `gain` lesson:
    the metric users can watch is the metric that keeps the harness on)."""
    import json as _json

    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    path = store.audit_dir / "telemetry.jsonl"
    per_op: dict[str, dict[str, int]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ev = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            op = str(ev.get("op") or "?")
            slot = per_op.setdefault(op, {"events": 0, "raw": 0, "emitted": 0})
            slot["events"] += 1
            slot["raw"] += int(ev.get("raw_bytes", 0))
            slot["emitted"] += int(ev.get("emitted_bytes", 0))
    if not per_op:
        print("no telemetry yet — run some commands under the harness first")
        return 1
    total_raw = sum(s["raw"] for s in per_op.values())
    total_emitted = sum(s["emitted"] for s in per_op.values())
    saved_tok = max(0, (total_raw - total_emitted) // 4)
    print(f"[ctx gain · workspace {ws.workspace_id[:12]}]")
    print(
        f"contained: {total_raw:,} bytes raw -> {total_emitted:,} bytes emitted "
        f"({total_raw / max(1, total_emitted):.1f}x)"
    )
    print(f"est tokens kept out of context: {saved_tok:,}")
    # Input-price framing only; the real savings compound through cache reads.
    # Price against the session's own model when the proxy recorded one
    # (host-neutral: works for Gemini/GPT/Claude alike); otherwise show a
    # cheap->premium band from the shipped table rather than naming one vendor.
    from ctx import pricing
    from ctx.engagement import session_model

    model = session_model(ws.root)
    if model:
        p = pricing.price_for(model, workspace_root=ws.root)
        print(
            f"est spend avoided (input-priced): ~${saved_tok * p.input / 1e6:.2f} "
            f"({model} @ ${p.input:g}/Mtok in)"
        )
    else:
        tbl = pricing.load_table(ws.root)
        ins = sorted(float(r.get("in", 0)) for r in tbl["models"] if float(r.get("in", 0)) > 0)
        lo, hi = (ins[0], ins[-1]) if ins else (1.0, 15.0)
        print(
            f"est spend avoided (input-priced): ~${saved_tok * lo / 1e6:.2f}–"
            f"${saved_tok * hi / 1e6:.2f} (economy–premium input rates)"
        )
    print("by verb:")
    for op, s in sorted(per_op.items(), key=lambda kv: -kv[1]["raw"]):
        ratio = s["raw"] / max(1, s["emitted"])
        print(
            f"  {op:7s} {s['events']:>5,} events · {s['raw']:>12,} B -> "
            f"{s['emitted']:>10,} B ({ratio:.1f}x)"
        )
    return 0


def _delivery_plan(ws, *, outcome: str, family: str, base_tokens: int, signature=None):
    """One resolver for every emission budget (docs/EDC.md §13, LADDERS
    edge 8): outcome + circuit + signal record + config in, DeliveryPlan
    out. Fail-open inside the resolver by contract."""
    from ctx import resolver

    return resolver.resolve_delivery(
        outcome,
        family,
        contract_rendering={"base_tokens": base_tokens},
        session=resolver.session_state(ws.root, signature),
        environment=resolver.environment_signals(ws.root),
        config_budgets=ws.config.budgets,
    )


def _emit_bounded_digest(ws, store, text: str, plan) -> None:
    """Shared emission boundary: engagement filtering (teaching prose obeys
    both the plan and the graduated-engagement cap), the plan's token
    budget as the bounded() backstop, and the plan receipt telemetry."""
    from ctx import resolver
    from ctx.engagement import filter_digest, suggestion_cap
    from ctx.textutil import bounded

    eng = ws.config.engagement
    cap = suggestion_cap(ws.root, mode=eng.mode, lean_models=eng.lean_models)
    if not plan.include_teaching:
        cap = 0  # include_teaching=False maps to suggestion cap 0
    resolver.record_plan_receipt(store.audit_dir if store is not None else None, plan)
    print(bounded(filter_digest(text, cap), plan.token_budget))


def _emit_run_digest(ws, digest: str, manifest: dict, store=None, signature=None) -> int:
    """Shared emission tail for foreground runs and finalized background
    jobs: delivery-plan resolution (budget selection, failure asymmetry,
    window pressure), engagement filtering, and the run's exit-code
    semantics (124 timeout, 3 nonzero, 0 success)."""
    # Zero-hop inline digests may exceed the summary budget by design; the
    # result budget is the hard emission backstop either way.
    base = (
        ws.config.budgets.result_tokens
        if "output (complete):" in digest
        else ws.config.budgets.digest_tokens
    )
    # Failure asymmetry rides through the resolver: a failing run's output
    # is evidence, not boilerplate. exitCode != 0 covers None too: timeouts
    # and signal deaths are failures (docs/LADDERS.md edge 4 — parity with
    # eval's treatment).
    outcome = "success" if manifest["result"]["exitCode"] == 0 else "failure"
    plan = _delivery_plan(
        ws, outcome=outcome, family="run", base_tokens=base, signature=signature
    )
    # Graduated engagement (mechanism C): affordances are filtered at this
    # emission boundary only — the stored digest identity stays canonical.
    _emit_bounded_digest(ws, store, digest, plan)
    result = manifest["result"]
    if result["timedOut"]:
        return 124
    return 0 if result["exitCode"] == 0 else 3


def _cmd_run(ws, ns) -> int:
    from ctx.digest import render_run_digest
    from ctx.execution import ExecutionError, run_capture
    from ctx.store import Store

    command = list(ns.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("ctx run: no command given (use: ctx run -- <command> [args...])", file=sys.stderr)
        return 2

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)

    if ns.bg or ns.bg_after is not None:
        return _cmd_run_bg(ws, store, ns, command)

    try:
        capture = run_capture(
            ws,
            command,
            cwd=ns.cwd,
            shell=ns.shell,
            timeout=ns.timeout,
            store=store,
        )
    except ExecutionError as e:
        print(f"ctx run: {e}", file=sys.stderr)
        return 1

    # Reflex arc (docs/REFLEX.md layer 3): a signature already intervened on
    # this session re-arriving here IS the starvation loop — check_command
    # scores it (deduped against the hook's sighting of the same re-run) and
    # reports the densify latch. Latched → render the dense census and
    # declare it in the printed header. Fail-open: broken reflex state means
    # a plain digest, never a failed run.
    sig = None
    dense = False
    try:
        import shlex

        from ctx import reflex

        cmd_str = command[0] if ns.shell else shlex.join(command)
        sig = reflex.command_signature(cmd_str)
        if sig:
            dense = reflex.check_command(ws.root, cmd_str) == "densify" or (
                reflex.densify_latched(ws.root, sig)
            )
    except Exception:
        sig, dense = None, False

    # EDC phase 4: resolve the delivery plan and hand it to the renderer
    # when the digest layer accepts it (duck-typed `plan=` kwarg; absent →
    # legacy rendering, byte-identical by construction). The render-time
    # plan uses the digest base budget; the emission backstop re-resolves
    # with the actual zero-hop marker in `_emit_run_digest`.
    render_kwargs = {}
    try:
        import inspect

        if "plan" in inspect.signature(render_run_digest).parameters:
            outcome = (
                "success" if capture.manifest["result"]["exitCode"] == 0 else "failure"
            )
            render_kwargs["plan"] = _delivery_plan(
                ws,
                outcome=outcome,
                family="run",
                base_tokens=ws.config.budgets.digest_tokens,
                signature=sig,
            )
    except Exception:
        render_kwargs = {}

    digest, manifest = render_run_digest(
        store, ws, capture.manifest, focus=ns.focus, dense=dense, **render_kwargs
    )
    # A digest that omitted content is an intervention (hypothesis: the model
    # uses the digest, not a re-run). Record it so the reflex arc can score
    # the next command against it.
    try:
        if sig:
            from ctx import reflex

            if reflex.has_omissions(digest):
                short = str(manifest.get("id", "")).removeprefix("sha256:")[:12]
                reflex.note_intervention(
                    ws.root, sig, short, hints=reflex.count_hints(digest)
                )
    except Exception:
        pass
    if dense:
        # Printed declaration only — the stored digest identity/meta hash is
        # computed inside render_run_digest and never sees reflex state.
        from ctx.reflex import DENSIFY_HEADER

        digest = DENSIFY_HEADER + "\n" + digest
    return _emit_run_digest(ws, digest, manifest, store=store, signature=sig)


def _cmd_run_bg(ws, store, ns, command: list[str]) -> int:
    """`ctx run --bg / --bg-after T`: supervised launch, then a bounded
    patience window. Finished in time → the normal digest, byte-identical
    to a foreground run. Still running → job handle, exit 0."""
    from ctx.jobs import (
        JobError,
        backgrounded_status,
        finalize_job,
        start_job,
        wait_for_done,
    )
    from ctx.workspace import WorkspaceError

    patience = ns.bg_after if ns.bg_after is not None else 0.0  # --bg ⇒ 0
    try:
        job_id = start_job(
            ws, store, command,
            cwd=ns.cwd, shell=ns.shell, timeout=ns.timeout, focus=ns.focus,
        )
    except (JobError, WorkspaceError) as e:
        print(f"ctx run: {e}", file=sys.stderr)
        return 1
    try:
        if wait_for_done(store, job_id, timeout=max(0.0, patience)):
            digest, manifest = finalize_job(ws, store, job_id)
            return _emit_run_digest(ws, digest, manifest, store=store)
        print(backgrounded_status(store, job_id))
        return 0
    except JobError as e:
        print(f"ctx run: {e}", file=sys.stderr)
        return 1


def _cmd_job(ws, ns) -> int:
    from ctx.jobs import (
        JobError,
        finalize_job,
        job_state,
        job_status,
        kill_job,
        resolve_job_id,
        wait_for_done,
    )
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        job_id = resolve_job_id(store, ns.job_id)
        if ns.kill:
            digest, manifest = kill_job(ws, store, job_id)
            short = manifest["id"].removeprefix("sha256:")[:12]
            print(f"[ctx job:{job_id} killed · finalized → run:{short}]")
            _emit_run_digest(ws, digest, manifest, store=store)
            return 0
        if ns.wait:
            if not wait_for_done(store, job_id, timeout=ns.timeout):
                print(job_status(store, job_id, tail=ns.tail))
                return 124
            digest, manifest = finalize_job(ws, store, job_id)
            short = manifest["id"].removeprefix("sha256:")[:12]
            print(f"[ctx job:{job_id} finalized → run:{short}]")
            return _emit_run_digest(ws, digest, manifest, store=store)
        state = job_state(store, job_id)
        if state in ("done", "finalized"):
            digest, manifest = finalize_job(ws, store, job_id)
            short = manifest["id"].removeprefix("sha256:")[:12]
            print(f"[ctx job:{job_id} finalized → run:{short}]")
            _emit_run_digest(ws, digest, manifest, store=store)
            return 0
        status = job_status(store, job_id, tail=ns.tail)
        print(status)
        return 1 if state == "failed" else 0
    except JobError as e:
        print(f"ctx job: {e}", file=sys.stderr)
        return 1


def _cmd_jobs(ws) -> int:
    from ctx.jobs import list_jobs
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(list_jobs(store))
    return 0


def _cmd_eval(ws, ns) -> int:
    from ctx.execution import ExecutionError
    from ctx.pyeval import run_eval
    from ctx.store import Store

    if ns.file:
        full = ws.confine(ns.file, must_exist=True)
        rel = ws.relativize(full)
        if ws.is_ignored(rel):
            print(f"ctx eval: path is excluded from capture by policy: {rel}", file=sys.stderr)
            return 1
        script = full.read_text(encoding="utf-8")
    elif ns.script in (None, "-"):
        if sys.stdin.isatty():
            print(
                "ctx eval: no script given (pass text, --file <path>, or pipe stdin)",
                file=sys.stderr,
            )
            return 2
        script = sys.stdin.read()
    else:
        script = ns.script

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        text, code = run_eval(
            ws, store, script, timeout=ns.timeout, cwd=ns.cwd, focus=ns.focus
        )
    except ExecutionError as e:
        print(f"ctx eval: {e}", file=sys.stderr)
        return 1

    # Delivery plan (EDC §13): zero-hop inline uses the result budget;
    # failure asymmetry (a failing script's traceback is evidence — timeout
    # 124 included, docs/LADDERS.md edge 4) and window pressure compose in
    # the resolver, floor-protected.
    base = (
        ws.config.budgets.result_tokens
        if "output (complete):" in text
        else ws.config.budgets.digest_tokens
    )
    plan = _delivery_plan(
        ws,
        outcome="success" if code == 0 else "failure",
        family="eval",
        base_tokens=base,
    )
    _emit_bounded_digest(ws, store, text, plan)
    if code == 124:
        return 124
    return 0 if code == 0 else 3


def _emit_retrieval(ws, store, out: str) -> int:
    """Shared emission tail for every retrieval-path verb (search/get/
    stats/diff/map/code): the ONE budget choke point (LADDERS edge 8).
    ``resolve_retrieval_budget`` returns exactly the configured
    turn-retrieval budget today — the same value ``charge_turn_budget``
    enforces — so behavior is unchanged; the window-pressure hook-in for
    retrieval lands in the resolver, not in seven call sites."""
    from ctx import resolver
    from ctx.retrieval import charge_turn_budget

    resolver.resolve_retrieval_budget(ws.config, resolver.environment_signals(ws.root))
    warning = charge_turn_budget(store, ws, out)
    if warning:
        print(warning)
    print(out)
    return 0


def _cmd_diff(ws, ns) -> int:
    from ctx.retrieval import RetrievalError
    from ctx.rundiff import run_diff
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        out = run_diff(store, ws, ns.ref_a, ns.ref_b)
    except RetrievalError as e:
        print(f"ctx diff: {e}", file=sys.stderr)
        return 1
    return _emit_retrieval(ws, store, out)


def _cmd_map(ws, ns) -> int:
    from ctx import resolver
    from ctx.repomap import repo_map
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    # The map's explicit --budget routes through the same resolver choke
    # point (today: returned verbatim; pressure hook-in comes later).
    budget = resolver.resolve_retrieval_budget(
        ws.config, resolver.environment_signals(ws.root), requested=ns.budget
    )
    out = repo_map(store, ws, budget=budget, focus=ns.focus)
    return _emit_retrieval(ws, store, out)


def _cmd_code(ws, ns) -> int:
    from ctx.codeverbs import cmd_def, cmd_diag, cmd_refs
    from ctx.retrieval import RetrievalError
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        if ns.cmd == "def":
            out = cmd_def(store, ws, ns.target)
        elif ns.cmd == "refs":
            out = cmd_refs(store, ws, ns.symbol, ns.path)
        else:
            out = cmd_diag(store, ws, ns.path)
    except RetrievalError as e:
        print(f"ctx {ns.cmd}: {e}", file=sys.stderr)
        return 1
    return _emit_retrieval(ws, store, out)


def _cmd_q(ws, ns) -> int:
    """`ctx q '<stage> | …'` — the M-H composition algebra (docs/ALGEBRA.md).
    Total by construction (no loops, ≤8 stages), so its cost is statically
    boundable — the property that makes it MCP-tier-safe later (no MCP
    wiring this wave). Emission rides the same engagement filter + bounded
    backstop as the other verbs."""
    from ctx.query import run_query
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    text, code = run_query(ws, store, ns.query, trace=ns.trace)
    if code != 0:
        print(text, file=sys.stderr)
        return code
    plan = _delivery_plan(
        ws, outcome="success", family="q",
        base_tokens=ws.config.budgets.result_tokens,
    )
    _emit_bounded_digest(ws, store, text, plan)
    return 0


def _read_plan_text(ws, plan_file: str) -> str | None:
    """Plan JSON from a workspace file or stdin ('-'). None ⇒ usage error
    (message already printed)."""
    if plan_file in (None, "-"):
        if sys.stdin.isatty():
            print(
                "ctx plan: no plan given (pass a JSON path or pipe stdin)",
                file=sys.stderr,
            )
            return None
        return sys.stdin.read()
    full = ws.confine(plan_file, must_exist=True)
    return full.read_text(encoding="utf-8")


def _cmd_plan(ws, ns) -> int:
    """`ctx plan validate|price|run|ops` — compiled evidence plans
    (docs/EVIDENCE-PLANS.md). Validation and pricing are static: nothing
    executes; `run` executes the DAG and emits one investigation digest."""
    from ctx import plan_ir, plan_ops
    from ctx.store import Store

    if ns.plan_cmd == "ops":
        print(plan_ops.ops_census())
        return 0

    text = _read_plan_text(ws, ns.plan_file)
    if text is None:
        return 2

    if ns.plan_cmd in ("validate", "price"):
        try:
            plan = plan_ir.parse_plan(text)
        except plan_ir.PlanError as e:
            print(f"ctx plan: {e}", file=sys.stderr)
            return 2
        rejections = plan_ir.validate_plan(plan, tier="cli", plan_policy=ws.config.plan)
        if rejections:
            print(f"[ctx plan · REJECTED · {len(rejections)} problem(s)]")
            for r in rejections:
                print("  " + r.render())
            return 2
        if ns.plan_cmd == "validate":
            print(
                f"[ctx plan · OK · {len(plan.steps)} nodes · "
                f"plan:{plan.plan_id()[:12]}]"
            )
            return 0
        print(plan_ir.price_plan(plan))
        if getattr(ns, "plan_value_explain", False):
            from ctx import plan_value as pv

            floors = pv.required_floors(plan.objective_kind, plan.requires)
            candidates = [
                pv.CandidateAction(
                    op=st.op,
                    cost_class=plan_ops.OPS[st.op].cost,
                    klass=plan_ops.OPS[st.op].klass,
                )
                for st in plan.steps
                if st.op in plan_ops.OPS
            ]
            priors = pv.load_priors(ws)
            ranked = pv.rank_followup(candidates, priors.get("operators", priors))
            declared = plan.steps[0].op if plan.steps else None
            print()
            print(pv.render_shadow(declared, ranked, floors=floors))
        return 0

    # run
    from ctx.plan_exec import execute_plan

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    out, code = execute_plan(ws, store, text, tier="cli")
    if code == 2:
        print(out)
        return 2
    _emit_investigation(ws, store, out)
    return code


def _emit_investigation(ws, store, text: str) -> None:
    """Emission tail for plan/investigate digests: the shared resolver
    (family 'investigate'; a digest naming failed nodes rides the failure
    budget) + engagement filter + bounded backstop."""
    outcome = "failure" if ("ERROR:" in text or "candidates (census): 0" not in text) else "success"
    plan = _delivery_plan(
        ws,
        outcome=outcome,
        family="investigate",
        base_tokens=ws.config.budgets.result_tokens,
    )
    _emit_bounded_digest(ws, store, text, plan)


def _cmd_investigate(ws, ns) -> int:
    """`ctx investigate <plan.json>` — one hypothesis epoch. Same execution
    as `ctx plan run`, plus the epochal-control ledger: replans beyond the
    budget are declared with a banner and recorded for the reflex plane
    (the asymmetric-loss doctrine: warn and record, never block local
    evidence-gathering)."""
    import hashlib as _hashlib
    import json as _json

    from ctx import plan_ir
    from ctx.plan_exec import execute_plan
    from ctx.store import Store

    text = _read_plan_text(ws, ns.plan_file)
    if text is None:
        return 2
    try:
        plan = plan_ir.parse_plan(text)
    except plan_ir.PlanError as e:
        print(f"ctx investigate: {e}", file=sys.stderr)
        return 2

    replans = ns.replans if ns.replans is not None else ws.config.plan.replans
    objective_key = _hashlib.sha256(
        " ".join(plan.question.lower().split()).encode("utf-8")
    ).hexdigest()[:12]
    ledger_dir = ws.root / ".ctx-session-reads"
    ledger = ledger_dir / "investigations.jsonl"
    prior = 0
    try:
        if ledger.is_file():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try:
                    ev = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if ev.get("objective") == objective_key:
                    prior += 1
    except OSError:
        pass

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    node_rows: dict[str, int] = {}
    out, code = execute_plan(ws, store, text, tier="cli", node_rows=node_rows)
    if code == 2:
        print(out)
        return 2

    try:
        import time as _time

        ledger_dir.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(
                _json.dumps(
                    {
                        "op": "investigate",
                        "objective": objective_key,
                        "epoch": prior + 1,
                        "plan": plan.plan_id()[:12],
                        "ts": _time.time(),  # operational only
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass

    if prior > replans:
        out += (
            f"\nreplan budget: epoch {prior + 1} for this objective exceeds the "
            f"allowance ({replans} replan(s)) — unlimited replanning degenerates "
            "to the interactive loop; patch/verify or change the hypothesis"
        )
    _emit_investigation(ws, store, out)
    if getattr(ns, "inv_advise", False):
        print()
        print(_investigate_advice(ws, plan, node_rows))
    return code


def _cmd_ask(ws, ns) -> int:
    """`ctx ask "<question>" --intent <intent>` — compile a typed intent
    preset into one ctx.plan/v1 and execute it through the SAME executor
    and emission tail as investigate. No natural-language parser: the
    subject is a flag or the question's sole identifier token (disclosed);
    a missing/ambiguous slot is a teaching error that suggests, never
    acts. The disclosure rides ABOVE the digest so the interpretation is
    always visible (never hidden behind --trace)."""
    from ctx import ask
    from ctx.plan_exec import execute_plan
    from ctx.store import Store

    try:
        plan_json, disclosure = ask.compile_ask(
            ns.ask_intent,
            ns.question,
            symbol=ns.ask_symbol,
            run=ns.ask_run,
            depth=ns.ask_depth,
            ref_a=ns.ask_run,
            ref_b=getattr(ns, "ask_against", None),
            command=getattr(ns, "ask_command", None),
        )
    except ask.AskError as e:
        print(str(e), file=sys.stderr)
        return 2

    header = "\n".join(f"  {ln}" for ln in disclosure)
    if getattr(ns, "ask_show_plan", False):
        print(f"[ctx ask]\n{header}\n")
        print(plan_json)
        return 0

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    out, code = execute_plan(ws, store, plan_json, tier="cli")
    if code == 2:  # a validation rejection: the text IS the typed reason
        print(f"[ctx ask]\n{header}")
        print(out)
        return 2
    # Disclosure first, then the shared investigation emission tail.
    print(f"[ctx ask]\n{header}")
    _emit_investigation(ws, store, out)
    return code


def _investigate_advice(ws, plan, node_rows=None) -> str:
    """Shadow follow-up report for `ctx investigate --advise` (report only —
    never reorders, inserts, or suppresses anything). Shows the declared
    plan's first op against what the empirical follow-up ordering would have
    preferred among the same already-applicable candidates, with the full
    lexicographic reason; appends one ctx.shadow-rank/v1 line to the shadow
    ledger so the paired referee can score agreement offline. Floors are
    displayed descriptively from REALIZED coverage (an op's declared
    `provides` counts only when its node produced rows). Fail-open."""
    try:
        import json as _json
        import time as _time

        from ctx import plan_ops
        from ctx import plan_value as pv

        floors = pv.required_floors(plan.objective_kind, plan.requires)
        coverage = pv.realized_coverage(plan.steps, node_rows or {})
        declared_first = plan.steps[0].op if plan.steps else None
        # Hard constraints FIRST: candidates are the plan's own declared ops
        # plus registered engine-available observe-class ops — the ranking
        # never introduces an action the tier could not run.
        ran_ops = [s.op for s in plan.steps]
        names = list(dict.fromkeys(ran_ops)) + sorted(
            name
            for name, spec in plan_ops.OPS.items()
            if name not in ran_ops
            and (spec.probe_available is None or spec.probe_available())
        )
        candidates = [
            pv.CandidateAction(
                op=n,
                cost_class=plan_ops.OPS[n].cost,
                klass=plan_ops.OPS[n].klass,
            )
            for n in names
            if n in plan_ops.OPS
        ]
        priors = pv.load_priors(ws)
        ranked = pv.rank_followup(candidates, priors.get("operators", priors))
        report = pv.render_shadow(
            declared_first, ranked, floors=floors, coverage=coverage
        )
        # Shadow ledger: the paired referee's input. Operational ts only.
        try:
            ldir = ws.root / ".ctx-session-reads"
            ldir.mkdir(parents=True, exist_ok=True)
            with (ldir / "shadow-rank.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    _json.dumps(
                        {
                            "schema": "ctx.shadow-rank/v1",
                            "plan": plan.plan_id()[:12],
                            "declared_first": declared_first,
                            "shadow_first": ranked[0].op if ranked else None,
                            "agreement": bool(
                                ranked and declared_first == ranked[0].op
                            ),
                            "ts": _time.time(),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        except OSError:
            pass
        return report
    except Exception as e:  # report only: never fail the investigation
        return f"(follow-up shadow report unavailable: {type(e).__name__})"


def _cmd_policy(ws, ns) -> int:
    if ns.policy_cmd == "show":
        path = ws.root / "ctx-policy.toml"
        if path.is_file():
            print(path.read_text(encoding="utf-8"))
        else:
            print("no compiled policy")
        return 0
    try:
        from ctx.policy import compile_policy, render_policy, write_policy
    except ImportError:
        print("policy module not available", file=sys.stderr)
        return 1
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    kwargs = {"min_runs": ns.min_runs} if ns.min_runs is not None else {}
    if getattr(ns, "plan_value", False):
        from ctx.policy import compile_plan_value

        kwargs["plan_value"] = compile_plan_value(ws)
    policy = compile_policy(store, ws, **kwargs)
    print(render_policy(policy))
    print(f"written: {write_policy(ws, policy)}")
    return 0


def _cmd_retrieval(ws, ns, verb: str) -> int:
    from ctx.retrieval import RetrievalError, Selector, _span, get, search, stats
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        if verb == "search":
            out = search(
                store,
                ws,
                ns.ref,
                ns.patterns,
                fixed=ns.fixed,
                mode_all=ns.all,
                context=ns.context,
                glob=ns.glob,
                scope=ns.scope,
                max_matches=ns.max_matches,
            )
        elif verb == "get":
            selector = Selector(
                lines=_span(ns.lines) if ns.lines else None,
                bytes=_span(ns.bytes) if ns.bytes else None,
                records=_span(ns.records) if ns.records else None,
                json_pointer=ns.json_pointer,
                symbol=ns.symbol,
                span=ns.span,
            )
            out = get(store, ws, ns.ref, selector)
        else:
            out = stats(store, ws, ns.ref, scope=ns.scope)
    except RetrievalError as e:
        print(f"ctx {verb}: {e}", file=sys.stderr)
        return 1

    return _emit_retrieval(ws, store, out)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
