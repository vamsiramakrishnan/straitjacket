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
    if len(args) >= 3 and args[0] == "hook" and args[1] in ("antigravity", "claude-code"):
        if args[2] == "pre-tool-use":
            from ctx.hook import main_pre_tool_use

            return main_pre_tool_use(flavor=args[1])
        # Unknown hook stage: still emit exactly one valid decision.
        sys.stdout.write('{"decision":"allow"}\n')
        return 0

    if args and args[0] == "mcp":
        from ctx.mcp import serve

        return serve(bounded_only="--bounded-only" in args)

    return _main_slow(args)


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

    p_stats = sub.add_parser("stats", help="bounded schema/shape statistics")
    p_stats.add_argument("ref", nargs="?", default="repo:")
    p_stats.add_argument("--scope", help="named monorepo scope")

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

    p_wrap = sub.add_parser("wrap", help="run one agent session under the harness")
    p_wrap.add_argument("host", choices=["claude", "antigravity"])
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
        if ns.cmd == "antigravity" and ns.agy_cmd == "install":
            ws = resolve_workspace(ns.workspace or ns.agy_workspace)
            from ctx.installer import install_antigravity

            print(install_antigravity(ws))
            return 0

        if ns.cmd == "wrap":
            from ctx.wrap import print_config, wrap_antigravity, wrap_claude

            agent_args = list(ns.agent_args)
            # REMAINDER swallows options placed after the host positional;
            # recognize --print-config there too (but never past the `--`).
            if "--print-config" in agent_args:
                tail = agent_args.index("--") if "--" in agent_args else len(agent_args)
                if agent_args.index("--print-config") < tail:
                    ns.print_config = True
                    agent_args.remove("--print-config")
            if ns.print_config:
                print(print_config(ns.host))
                return 0
            ws = resolve_workspace(ns.workspace)
            if agent_args and agent_args[0] == "--":
                agent_args = agent_args[1:]
            if ns.host == "claude":
                return wrap_claude(ws.root, agent_args)
            return wrap_antigravity(ws.root)

        ws = resolve_workspace(ns.workspace)

        if ns.cmd == "run":
            return _cmd_run(ws, ns)
        if ns.cmd == "search":
            return _cmd_retrieval(ws, ns, "search")
        if ns.cmd == "get":
            return _cmd_retrieval(ws, ns, "get")
        if ns.cmd == "stats":
            return _cmd_retrieval(ws, ns, "stats")
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

    digest, manifest = render_run_digest(store, ws, capture.manifest, focus=ns.focus)
    from ctx.textutil import bounded

    # Zero-hop inline digests may exceed the summary budget by design; the
    # result budget is the hard emission backstop either way.
    budget = (
        ws.config.budgets.result_tokens
        if "output (complete):" in digest
        else ws.config.budgets.digest_tokens
    )
    print(bounded(digest, budget))
    result = manifest["result"]
    if result["timedOut"]:
        return 124
    return 0 if result["exitCode"] == 0 else 3


def _cmd_retrieval(ws, ns, verb: str) -> int:
    from ctx.retrieval import RetrievalError, Selector, _span, charge_turn_budget, get, search, stats
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

    warning = charge_turn_budget(store, ws, out)
    if warning:
        print(warning)
    print(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
