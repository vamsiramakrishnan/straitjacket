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
        if args[2] == "post-tool-use":
            from ctx.hook import main_post_tool_use

            return main_post_tool_use(flavor=args[1])
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

    p_seq = sub.add_parser("seq", help="declared command tree: N steps, one round")
    p_seq.add_argument("steps", nargs="+", help="shell command strings, run in order")
    p_seq.add_argument("--keep-going", action="store_true", dest="keep_going",
                       help="run remaining steps after a failure (default: halt)")
    p_seq.add_argument("--timeout", type=float, help="per-step timeout seconds")
    p_seq.add_argument("--focus", help="bias step digests toward this question")

    sub.add_parser("gain", help="cumulative token/cost savings from telemetry")

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

    p_policy = sub.add_parser("policy", help="compiled steering policy")
    pol_sub = p_policy.add_subparsers(dest="policy_cmd", required=True)
    p_pc = pol_sub.add_parser("compile", help="compile policy from telemetry")
    p_pc.add_argument("--min-runs", type=int, dest="min_runs")
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
            from ctx.wrap import print_config, wrap_antigravity, wrap_claude

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
                print(print_config(ns.host))
                return 0
            ws = resolve_workspace(ns.workspace)
            if agent_args and agent_args[0] == "--":
                agent_args = agent_args[1:]
            if ns.host == "claude":
                return wrap_claude(
                    ws.root, agent_args, use_proxy=use_proxy, rescue_pct=rescue_pct
                )
            return wrap_antigravity(ws.root)

        ws = resolve_workspace(ns.workspace)

        if ns.cmd == "run":
            return _cmd_run(ws, ns)
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
            from ctx.textutil import bounded as _bounded

            store = _Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            text, code = run_seq(
                ws, store, ns.steps,
                halt_on_fail=not ns.keep_going,
                timeout=ns.timeout, focus=ns.focus,
            )
            budget = ws.config.budgets.result_tokens
            if code != 0:
                budget = int(budget * ws.config.budgets.failure_budget_factor)
            print(_bounded(text, budget))
            return 0 if code == 0 else 3
        if ns.cmd == "gain":
            return _cmd_gain(ws)
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
    print(
        f"est spend avoided (input-priced): ~${saved_tok * 3 / 1e6:.2f} sonnet · "
        f"~${saved_tok * 1 / 1e6:.2f} haiku"
    )
    print("by verb:")
    for op, s in sorted(per_op.items(), key=lambda kv: -kv[1]["raw"]):
        ratio = s["raw"] / max(1, s["emitted"])
        print(
            f"  {op:7s} {s['events']:>5,} events · {s['raw']:>12,} B -> "
            f"{s['emitted']:>10,} B ({ratio:.1f}x)"
        )
    return 0


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
    # Failure asymmetry: a failing run's output is evidence, not boilerplate.
    if manifest["result"]["exitCode"] not in (0, None):
        budget = int(budget * ws.config.budgets.failure_budget_factor)
    # Graduated engagement (mechanism C): affordances are filtered at this
    # emission boundary only — the stored digest identity stays canonical.
    from ctx.engagement import filter_digest, suggestion_cap

    eng = ws.config.engagement
    cap = suggestion_cap(ws.root, mode=eng.mode, lean_models=eng.lean_models)
    print(bounded(filter_digest(digest, cap), budget))
    result = manifest["result"]
    if result["timedOut"]:
        return 124
    return 0 if result["exitCode"] == 0 else 3


def _cmd_diff(ws, ns) -> int:
    from ctx.retrieval import RetrievalError, charge_turn_budget
    from ctx.rundiff import run_diff
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        out = run_diff(store, ws, ns.ref_a, ns.ref_b)
    except RetrievalError as e:
        print(f"ctx diff: {e}", file=sys.stderr)
        return 1
    warning = charge_turn_budget(store, ws, out)
    if warning:
        print(warning)
    print(out)
    return 0


def _cmd_map(ws, ns) -> int:
    from ctx.repomap import repo_map
    from ctx.retrieval import charge_turn_budget
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    out = repo_map(store, ws, budget=ns.budget, focus=ns.focus)
    warning = charge_turn_budget(store, ws, out)
    if warning:
        print(warning)
    print(out)
    return 0


def _cmd_code(ws, ns) -> int:
    from ctx.codeverbs import cmd_def, cmd_diag, cmd_refs
    from ctx.retrieval import RetrievalError, charge_turn_budget
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
    warning = charge_turn_budget(store, ws, out)
    if warning:
        print(warning)
    print(out)
    return 0


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
    policy = compile_policy(store, ws, **kwargs)
    print(render_policy(policy))
    print(f"written: {write_policy(ws, policy)}")
    return 0


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
