"""Coding agents this harness attaches to: wrap · orchestrate ·
antigravity · proxy.

All but `orchestrate` resolve their own workspace (or need none at
all), so they run before the dispatcher resolves one."""

from __future__ import annotations


def cmd_wrap(ns) -> int:
    """`ctx wrap <host>` — hook the harness into a coding agent. Resolves its
    own workspace, because `--print-config` must work outside one."""
    from ctx.workspace import resolve_workspace
    from ctx.wrap import (
        print_config,
        wrap_antigravity,
        wrap_claude,
        wrap_codex,
        wrap_detect,
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
    # Single-command multi-host setup: `setup` detects installed CLIs
    # and harnesses those; `all` forces every supported host.
    if ns.host in ("setup", "all"):
        return wrap_setup(ws.root, force_all=(ns.host == "all"))
    if ns.host == "codex":
        return wrap_codex(ws.root)
    if ns.host == "antigravity":
        return wrap_antigravity(ws.root)
    # claude: launch ephemerally when given agent args, else persist.
    if ns.host == "claude":
        if agent_args:
            return wrap_claude(
                ws.root, agent_args, use_proxy=use_proxy, rescue_pct=rescue_pct,
                orchestrate=use_orchestrate,
            )
        from ctx.installer import install_claude

        print(install_claude(resolve_workspace(str(ws.root))))
        print()
        print("Claude Code sessions in this workspace are now harnessed. "
              "For an ephemeral, zero-residue run instead: "
              "ctx wrap claude -- -p \"...\"")
        return 0
    return wrap_antigravity(ws.root)


def cmd_orchestrate(ws, ns) -> int:
    """`ctx orchestrate` — route a task's phases across installed harnesses
    by model cost. Usually a wrap mode rather than something a human types."""
    from ctx.orchestrator import orchestrate

    code, text = orchestrate(
        ws, ns.task, dry_run=ns.dry_run, force_run=ns.force_run
    )
    print(text)
    return code


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
