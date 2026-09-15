"""Find-and-read and code-navigation verbs: search · get · stats ·
diff · map · def · refs · diag · callers · callees · impact · q."""

from __future__ import annotations

import sys

from ctx.commands.emit import (
    _delivery_plan,
    _emit_bounded_digest,
    _emit_retrieval,
)


def _bad_input_errors() -> tuple[type[BaseException], ...]:
    """Delegates to the shared classification (commands/_errors.py).

    Kept as a lazy wrapper rather than a module-scope import: command
    modules keep their dependencies inside their functions so the hook's
    import graph stays off the hot path -- an invariant
    tests/test_cli_dispatch.py enforces, and which caught the first cut of
    this refactor.
    """
    from ctx.commands._errors import bad_input_errors

    return bad_input_errors()


def _fail(verb: str, e: BaseException) -> int:
    from ctx.commands._errors import fail

    return fail(verb, e)


def _retrieval(ws, ns, verb: str) -> int:
    from ctx.retrieval import Selector, _span, _span_anchored, get, search, stats
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    exact = False  # only `get --bytes` promises the caller exact bytes
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
            # One parse for the anchored grammar: the range and the anchor come
            # out of the same match, so a selector can never carry a range from
            # one spelling and an anchor from another.
            la, lb, lanchor = _span_anchored(ns.lines) if ns.lines else (None, None, None)
            selector = Selector(
                lines=(la, lb) if la is not None else None,
                lines_anchor=lanchor,
                hashlines=bool(getattr(ns, "hashlines", False)),
                snapcompact=bool(getattr(ns, "snapcompact", False)),
                bytes=_span(ns.bytes) if ns.bytes else None,
                records=_span(ns.records) if ns.records else None,
                json_pointer=ns.json_pointer,
                symbol=ns.symbol,
                span=ns.span,
            )
            out = get(store, ws, ns.ref, selector)
            exact = selector.bytes is not None
        else:
            out = stats(store, ws, ns.ref, scope=ns.scope)
    except _bad_input_errors() as e:
        return _fail(verb, e)

    return _emit_retrieval(ws, store, out, exact=exact)


def cmd_search(ws, ns) -> int:
    return _retrieval(ws, ns, "search")


def cmd_get(ws, ns) -> int:
    return _retrieval(ws, ns, "get")


def cmd_stats(ws, ns) -> int:
    """`ctx stats [--session]` — bounded shape statistics, or the session's
    wire scorecard when the proxy recorded one."""
    if getattr(ns, "session", False):
        from ctx.proxywindow import PROXY_SUBDIR
        from ctx.scorecard import compute_scorecard, render_scorecard
        from ctx.sessiondir import session_reads_path

        sc = compute_scorecard(session_reads_path(ws.root, PROXY_SUBDIR))
        if sc is None:
            print(
                "no wire observations for this workspace "
                "(run under `ctx wrap claude --proxy`)"
            )
            return 1
        print(render_scorecard(sc))
        return 0
    return _retrieval(ws, ns, "stats")


def cmd_diff(ws, ns) -> int:
    from ctx.rundiff import run_diff
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    exact = False  # only `get --bytes` promises the caller exact bytes
    try:
        out = run_diff(store, ws, ns.ref_a, ns.ref_b)
    except _bad_input_errors() as e:
        return _fail("diff", e)
    return _emit_retrieval(ws, store, out)


def cmd_map(ws, ns) -> int:
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


def _code(ws, ns) -> int:
    """def · refs · diag share one store, one error shape (the verb comes from
    ns.cmd) and one emission tail."""
    from ctx.codeverbs import cmd_def as _def
    from ctx.codeverbs import cmd_diag as _diag
    from ctx.codeverbs import cmd_refs as _refs
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    exact = False  # only `get --bytes` promises the caller exact bytes
    try:
        if ns.cmd == "def":
            out = _def(store, ws, ns.target)
        elif ns.cmd == "refs":
            out = _refs(store, ws, ns.symbol, ns.path)
        else:
            out = _diag(store, ws, ns.path)
    except _bad_input_errors() as e:
        return _fail(ns.cmd, e)
    return _emit_retrieval(ws, store, out)


def cmd_def(ws, ns) -> int:
    return _code(ws, ns)


def cmd_lsp(ws, ns) -> int:
    """`ctx lsp def|refs|hover <target>`: the language-server tier, explicit.
    Exit 2 when no server exists for the file's language (an invocation this
    machine cannot serve, not a ctx failure) and 3 when the server failed."""
    import re

    from ctx import lsp as _lsp
    from ctx.store import Store
    from ctx.textutil import EVIDENCE_LINE_CHARS

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    target = ns.target.strip()
    m = re.match(r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+)$", target)
    if m:
        rel, line, col = m.group("path"), int(m.group("line")), int(m.group("col"))
        rel = rel.removeprefix("repo:")
    else:
        m = re.match(r"^repo:(?P<path>.+?):(?P<sym>[A-Za-z_][\w.]*)$", target)
        if not m:
            print(f"ctx lsp: unparseable target {target!r}; use <path>:<line>:<col> or "
                  "repo:<path>:<Symbol>", file=sys.stderr)
            return 2
        rel = m.group("path")
        pos = _lsp.symbol_position(ws.root, rel, m.group("sym"))
        if pos is None:
            print(f"ctx lsp: symbol {m.group('sym')!r} not in the skeleton of {rel}", file=sys.stderr)
            return 2
        line, col = pos
    try:
        ws.confine(rel, must_exist=True)
    except Exception as e:  # noqa: BLE001 - the confinement error is the message
        print(f"ctx lsp: {e}", file=sys.stderr)
        return 2
    what = {"def": "definition", "refs": "references", "hover": "hover"}[ns.what]
    from ctx.bounds import explicit

    try:
        answer, server = _lsp.query(
            ws.root, rel, line, col, what,
            timeout=float(explicit(ns.timeout, _lsp.DEFAULT_TIMEOUT_S)),  # type: ignore[arg-type]
        )
    except _lsp.LspError as e:
        print(f"ctx lsp: {e}", file=sys.stderr)
        return 2 if "no language server" in str(e) else 3
    head = f"[ctx lsp {ns.what} {rel}:L{line}:C{col} · engine lsp ({server})]"
    if what == "hover":
        body = (answer or "").strip() or "(no hover information)"
        out = head + "\n" + body
    else:
        sites = answer or []
        lines = [head]
        lines.extend(f"repo:{r}:L{ln}: {text[:EVIDENCE_LINE_CHARS]}" for r, ln, text in sites)
        lines.append(f"coverage:\n  sites: {len(sites)}")
        out = "\n".join(lines)
    return _emit_retrieval(ws, store, out)


def cmd_refs(ws, ns) -> int:
    return _code(ws, ns)


def cmd_diag(ws, ns) -> int:
    return _code(ws, ns)


def cmd_callers(ws, ns) -> int:
    from ctx.callgraph import cmd_callers as _callers
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(_callers(store, ws, ns.symbol, unscoped=getattr(ns, "unscoped", False)))
    return 0


def cmd_callees(ws, ns) -> int:
    from ctx.callgraph import cmd_callees as _callees
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(_callees(store, ws, ns.symbol, unscoped=getattr(ns, "unscoped", False)))
    return 0


def cmd_impact(ws, ns) -> int:
    from ctx.callgraph import cmd_impact as _impact
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(
        _impact(
            store, ws, ns.symbol, depth=ns.depth, unscoped=getattr(ns, "unscoped", False)
        )
    )
    return 0


def cmd_cycles(ws, ns) -> int:
    from ctx.callgraph import cmd_cycles as _cycles
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(
        _cycles(
            store,
            ws,
            calls=getattr(ns, "calls", False),
            unscoped=getattr(ns, "unscoped", False),
        )
    )
    return 0


def cmd_impls(ws, ns) -> int:
    from ctx.callgraph import cmd_impls as _impls
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    out = _scip_impls(store, ws, ns.symbol)
    print(out if out is not None else _impls(store, ws, ns.symbol, depth=ns.depth))
    return 0


def _scip_impls(store, ws, symbol: str) -> str | None:
    """The exact rung of `ctx impls`: the index's own implementation edges,
    when an index answers. None hands the question to the call graph."""
    try:
        from ctx import scip_ingest

        got = scip_ingest.implementations(ws, symbol, store=store)
    except Exception:
        return None
    if got is None:
        return None
    sites, stale = got
    label = "scip (exact)"
    if stale:
        label += f" · {len(stale)} changed file{'s' if len(stale) != 1 else ''} not covered"
    lines = [f"[ctx impls {symbol} · engine {label}]", f"implementations: {len(sites)}"]
    lines += [f"  repo:{f}:L{ln}: {t}" for f, ln, t in sites]
    if stale:
        lines.append("changed since indexing (answer from ctx refs / the call graph): "
                     + ", ".join(stale[:6]) + (f" (+{len(stale) - 6})" if len(stale) > 6 else ""))
    return "\n".join(lines)


def cmd_pack(ws, ns) -> int:
    """`ctx pack "<task>"` — the turn-one context pack (docs/CODE-SEARCH.md)."""
    import json as _json

    from ctx import bounds
    from ctx.pack import DEFAULT_BUDGET_TOKENS, DEFAULT_MAX_FILES, build_pack, render_pack
    from ctx.store import Store

    task = ns.task
    if task == "-":
        task = sys.stdin.read()
    elif task.startswith("@"):
        try:
            task = ws.confine(task[1:], must_exist=True).read_text(encoding="utf-8")
        except (OSError, Exception) as e:  # confinement or read errors alike
            print(f"ctx pack: cannot read {task[1:]!r}: {e}", file=sys.stderr)
            return 2
    if not task.strip():
        print("ctx pack: the task text is empty", file=sys.stderr)
        return 2
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        pack = build_pack(
            store, ws, task,
            budget_tokens=bounds.count(bounds.explicit(ns.budget, DEFAULT_BUDGET_TOKENS)),
            max_files=bounds.count(bounds.explicit(ns.max_files, DEFAULT_MAX_FILES)),
            history=not ns.no_history,
        )
    except RuntimeError as e:
        print(f"ctx pack: {e}", file=sys.stderr)
        return 2
    if ns.as_json:
        print(_json.dumps(pack.payload(), indent=2, sort_keys=True))
        return 0
    return _emit_retrieval(ws, store, render_pack(pack))


def cmd_q(ws, ns) -> int:
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
