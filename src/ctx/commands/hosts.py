"""Coding agents this harness attaches to: wrap · orchestrate ·
antigravity · proxy.

All but `orchestrate` resolve their own workspace (or need none at
all), so they run before the dispatcher resolves one."""

from __future__ import annotations


def cmd_wrap(ns) -> int:
    """`ctx wrap <host>` — hook the harness into a coding agent. Resolves its
    own workspace, because `--print-config` must work outside one."""
    import sys

    from ctx.hosts import harnessable_hosts, host_by_name, wrapper_for
    from ctx.workspace import resolve_workspace
    from ctx.wrap import guided_setup, print_config, wrap_detect, wrap_setup

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
    use_orchestrate = False
    if "--orchestrate" in agent_args:
        tail = agent_args.index("--") if "--" in agent_args else len(agent_args)
        if agent_args.index("--orchestrate") < tail:
            use_orchestrate = True
            agent_args.remove("--orchestrate")
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
    if ns.host == "detect":
        return wrap_detect(ws.root, probe_version="--versions" in agent_args)
    if agent_args and agent_args[0] == "--":
        agent_args = agent_args[1:]

    wired = tuple(s.name for s in harnessable_hosts())
    spec = host_by_name(ns.host) if ns.host not in ("setup", "all") else None
    wrapper = wrapper_for(spec) if spec else None
    if ns.host not in ("setup", "all") and wrapper is None:
        # Registry-driven: a host that names no wrapper is not wrappable, and
        # saying so beats the old behaviour, where every unmatched name fell
        # through to `wrap_antigravity` — so `ctx wrap gemini` silently
        # harnessed a different agent.
        print(
            f"ctx wrap: {ns.host!r} is not a wrappable host "
            f"(wired: {', '.join(wired)}; `ctx wrap detect` shows the board)",
            file=sys.stderr,
        )
        return 2

    # --gateway: set up the host(s) AND wire the progressive-disclosure
    # gateway, so unrevealed MCP tool schemas never enter context.
    if use_gateway:
        from ctx.installer import install_gateway

        hosts = wired if ns.host in ("setup", "all") else (ns.host,)
        if ns.host in ("setup", "all"):
            setup_status = wrap_setup(ws.root)
        elif ns.host == "claude":
            setup_status = guided_setup(ws, hosts=["claude"])
        else:
            setup_status = wrapper(ws.root)
        if setup_status:
            return setup_status
        print()
        for h in hosts:
            print(install_gateway(resolve_workspace(str(ws.root)), h, apply=True))
            print()
        return 0
    # Single-command multi-host setup: `setup` detects installed CLIs
    # and harnesses those; `all` forces the three vendor-host integrations.
    if ns.host in ("setup", "all"):
        return wrap_setup(ws.root, force_all=(ns.host == "all"))
    # The one genuine per-host asymmetry, and it is about capability rather
    # than dispatch: only Claude Code supports an ephemeral, zero-residue
    # launch, so agent args mean "run it" there and the other hosts are
    # persistent installs whose wrapper takes the workspace alone.
    if ns.host == "claude":
        if agent_args:
            return wrapper(
                ws.root, agent_args, use_proxy=use_proxy, rescue_pct=rescue_pct,
                orchestrate=use_orchestrate,
            )
        return guided_setup(ws, hosts=["claude"])
    return wrapper(ws.root)


def cmd_setup(ns) -> int:
    """`ctx setup` — the short, human-facing alias for guided host setup."""
    import sys

    from ctx.installer import SETUP_HOSTS
    from ctx.wrap import wrap_setup
    from ctx.workspace import resolve_workspace

    hosts = list(ns.hosts or [])
    unknown = sorted(set(hosts) - set(SETUP_HOSTS))
    if unknown:
        print(
            f"ctx setup: unknown host(s): {', '.join(unknown)}; "
            f"choose from {', '.join(SETUP_HOSTS)}",
            file=sys.stderr,
        )
        return 2
    root = resolve_workspace(ns.workspace).root
    code = wrap_setup(root, hosts or None, force_all=bool(ns.all), force_repair=bool(ns.repair))
    if code == 0 and getattr(ns, "prune", False):
        # Bound before bloat, at the moment the harness is installed: the
        # same rule `ctx surface trim` recommends, made the default here.
        from ctx.prune import render_prune, run_prune
        from ctx.surface_profiles import HOSTS

        targets = tuple(h for h in (hosts or list(SETUP_HOSTS)) if h in HOSTS) or ("claude",)
        print(render_prune(run_prune(root, hosts=targets, apply=True)))
    return code


def cmd_orchestrate(ws, ns) -> int:
    """`ctx orchestrate` — route a task's phases across installed harnesses
    by model cost. Usually a wrap mode rather than something a human types.
    `--resume TASK` replays a task ledger instead of planning afresh."""
    from ctx.orchestrator import orchestrate

    # `resume` is passed only when set, so an `orchestrate` that predates the
    # task ledger (injected fakes included) keeps its signature.
    extra = {"resume": ns.resume} if getattr(ns, "resume", None) else {}
    code, text = orchestrate(
        ws, ns.task, dry_run=ns.dry_run, force_run=ns.force_run, **extra
    )
    print(text)
    return code


def cmd_task(ws, ns) -> int:
    """`ctx task ls|show|inbox|send` — read and write the task ledger.

    The ledger is the bus harnesses collaborate over (docs/TASK-LEDGER.md).
    `send` is the only writer here and it carries an ADDRESS, never content:
    the receiving node resolves it with `ctx get`."""
    import sys

    from ctx import taskledger as ledger

    if ns.task_cmd == "ls":
        ids = ledger.list_tasks(ws.root)
        if not ids:
            print("no task ledgers in this workspace (run `ctx orchestrate`)")
            return 0
        for tid in ids:
            st = ledger.task_state(ledger.load(ws.root, tid))
            done = sum(1 for n in st.nodes.values() if n.done)
            print(f"{tid}  nodes {done}/{len(st.nodes)} done  spent ${st.spent_usd:.4f}"
                  + ("" if st.cost_complete else " (partial)"))
        return 0
    if ns.task_cmd == "show":
        rows = ledger.load(ws.root, ns.task)
        if not rows:
            print(f"ctx task: unknown task {ns.task}", file=sys.stderr)
            return 2
        print(ledger.render_task(ledger.task_state(rows)))
        return 0
    if ns.task_cmd == "inbox":
        st = ledger.task_state(ledger.load(ws.root, ns.task))
        msgs = ledger.inbox_for(st, ns.node)
        if not msgs:
            print(f"inbox for {ns.node}: empty")
            return 0
        for m in msgs:
            note = f" — {m['note']}" if m.get("note") else ""
            print(f"from {m.get('from')}: {m.get('ref')}{note}")
        return 0
    if ns.task_cmd == "send":
        try:
            row = ledger.append(ws.root, ledger.inbox_row(
                ns.task, to=ns.node, sender=ns.sender, ref=ns.ref, note=ns.note,
            ))
        except ledger.LedgerError as e:
            print(f"ctx task send: {e}", file=sys.stderr)
            return 2
        print(f"sent to {row['to']}: {row['ref']}")
        return 0
    print(f"ctx task: unknown subcommand {ns.task_cmd}", file=sys.stderr)
    return 2


def cmd_antigravity(ns) -> int:
    """`ctx antigravity install` — render the repo-scoped plugin. Resolves
    its own workspace: the global `--workspace` falls back to the
    subcommand's own `--workspace`."""
    from ctx.installer import install_antigravity
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(ns.workspace or ns.agy_workspace)
    print(install_antigravity(ws))
    return 0


def cmd_proxy(ns) -> int:
    """`ctx proxy` — pass-through observer for host API traffic. Never
    resolves a workspace: the state dir is passed explicitly."""
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
