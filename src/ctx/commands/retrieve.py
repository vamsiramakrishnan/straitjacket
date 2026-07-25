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
    """The exception classes a retrieval verb must answer for itself.

    ``RetrievalError`` alone was not enough: ``UnknownIdError`` (and its
    sibling ``AmbiguousIdError``) subclass ``StoreError``, and ``parse_ref``
    raises ``RefError`` — so the single most common agent-facing mistake,
    `ctx get run:<id>` after a `ctx gc` or a retention expiry, fell through
    to cli.py's blanket handler and printed a bare ``ctx: …`` with no verb
    attribution."""
    from ctx.refs import RefError
    from ctx.retrieval import RetrievalError
    from ctx.store import StoreError

    return (RetrievalError, RefError, StoreError)


def _fail(verb: str, e: BaseException) -> int:
    """One error tail for every retrieval verb: attribute the failure to the
    verb the user typed, and return the documented exit code (docs/CLI.md,
    "Exit codes").

    Exit 2, not 1. All three of these classes mean the same thing to a
    calling script — *ctx rejected the invocation* — whether the argument was
    malformed (`--lines nope`), ungrammatical (`zzz:xyz`), or simply no
    longer resolves (a handle `ctx gc` collected). They used to split 1/2
    purely by which verb family caught them: `ctx get` said 1 for a bad
    selector while `ctx q` and argparse said 2 for the same class of
    mistake. 2 is the argparse convention and already the majority of this
    codebase's own usage errors, so 1 is left to mean only "ctx itself
    failed" — the blanket handler in cli.py."""
    print(f"ctx {verb}: {e}", file=sys.stderr)
    return 2


def _retrieval(ws, ns, verb: str) -> int:
    from ctx.retrieval import Selector, _span, get, search, stats
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
    except _bad_input_errors() as e:
        return _fail(verb, e)

    return _emit_retrieval(ws, store, out)


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


def cmd_refs(ws, ns) -> int:
    return _code(ws, ns)


def cmd_diag(ws, ns) -> int:
    return _code(ws, ns)


def cmd_callers(ws, ns) -> int:
    from ctx.callgraph import cmd_callers as _callers
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(_callers(store, ws, ns.symbol))
    return 0


def cmd_callees(ws, ns) -> int:
    from ctx.callgraph import cmd_callees as _callees
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(_callees(store, ws, ns.symbol))
    return 0


def cmd_impact(ws, ns) -> int:
    from ctx.callgraph import cmd_impact as _impact
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    print(_impact(store, ws, ns.symbol, depth=ns.depth))
    return 0


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
