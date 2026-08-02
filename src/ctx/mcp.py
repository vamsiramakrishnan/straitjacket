"""Bounded MCP retrieval server (SPEC §10.4).

Exposes exactly one stable tool schema with an ``op`` discriminator:
``search | get | stats | map | def | refs | diag | callers | callees | impact |
diff | repo | doctor | investigate``. Arbitrary command execution stays
on ``ctx run`` through the native command tool so the user's permission flow
remains visible; this server is bounded-only by construction —
``investigate`` accepts observe-class evidence plans only (execute-class
ops are typed rejections at tier='mcp').

Transport: MCP stdio — newline-delimited JSON-RPC 2.0.
"""

from __future__ import annotations

import collections
import json
import sys
from typing import Any

from ctx import __version__

PROTOCOL_VERSION = "2025-06-18"

#: Declared bounds for the MCP tool's ``maxTokens`` argument. Referenced by
#: BOTH the published schema and the runtime clamp: an advertised bound that
#: nothing enforces is worse than no bound at all.
_MAX_TOKENS_MIN = 64
_MAX_TOKENS_MAX = 4000

TOOL_SCHEMA: dict[str, Any] = {
    "name": "ctx",
    "description": (
        "Execute bounded retrieval against repository state or captured artifacts "
        "without placing unbounded output in model context. Ops: search (multi-pattern "
        "over run:/blob:/repo: refs), get (exact line/byte/record/json-pointer slices), "
        "stats (schema and repository shape), map (ranked budget-fitted codebase map), "
        "def (symbol definition site with snapshot + span), refs (reference sites for "
        "a symbol), diag (deterministic lint/syntax digest), "
        "callers (direct call-graph callers of a symbol), callees (direct call-graph "
        "callees of a symbol), impact (transitive callers of a symbol — blast radius, "
        "bounded depth), diff (regression delta between two captured run: refs), "
        "repo (workspace summary), doctor (health), investigate (one observe-class "
        "ctx.plan/v1 evidence plan executed as a single bounded digest)."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["op"],
        "properties": {
            "op": {
                "enum": [
                    "search", "get", "stats", "map",
                    "def", "refs", "diag", "callers", "callees", "impact",
                    "diff", "repo", "doctor", "investigate",
                ],
                "description": (
                    "callers/callees: direct call-graph edges for options.symbol; "
                    "impact: transitive callers (blast radius, options.depth<=6); "
                    "diff: regression delta between two run: refs (options.refA/refB); "
                    "investigate: execute an observe-class ctx.plan/v1 evidence plan "
                    "(options.plan, a JSON object) — total DAG, bounded, one digest; "
                    "execute-class ops (test.run, ast.rewrite.*) are CLI-only."
                ),
            },
            "workspace": {"type": "string", "description": "workspace path or alias"},
            "ref": {
                "type": "string",
                "description": "run:<id>[#stdout|#stderr] | blob:<id> | snapshot:<id> | repo:[path]",
            },
            "patterns": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 16,
                "description": "search patterns (regex by default)",
            },
            "selector": {
                "type": "object",
                "description": "get selector: {lines:'A:B'} | {bytes:'A:B'} | {records:'A:B'} | {jsonPointer:'/a/0'} | {symbol:'Cls.meth'} | {span:'<token from digest>'}",
            },
            "options": {
                "type": "object",
                "description": "search options: {fixed,all,context,glob,scope,maxMatches} · map options: {budget,focus} · def/refs/diag options: {target,symbol,path} · callers/callees/impact options: {symbol,depth} · diff options: {refA,refB}",
            },
            "maxTokens": {
                "type": "integer",
                "minimum": _MAX_TOKENS_MIN,
                "maximum": _MAX_TOKENS_MAX,
            },
        },
        "additionalProperties": False,
    },
}


# Long-lived server: cache workspace resolution + store connections so each
# tool call avoids re-spawning git subprocesses and reopening SQLite.
#
# Bounded LRU-with-TTL (S6 finding): an unbounded cache here holds one open
# sqlite connection per distinct workspace/alias ever seen by a long-lived
# server process — in a multi-workspace session (`ws:<alias>` roots) that
# grows without limit and never releases file descriptors. Eviction, either
# by TTL or by capacity, always closes the evicted Store before dropping it.
_WS_CACHE: "collections.OrderedDict[str | None, tuple[float, Any, Any]]" = (
    collections.OrderedDict()
)
_WS_CACHE_TTL = 10.0
_WS_CACHE_MAXSIZE = 8


def _evict_ws_cache_entry(key: str | None) -> None:
    entry = _WS_CACHE.pop(key, None)
    if entry is not None:
        _, _, store = entry
        try:
            store.close()
        except Exception:
            pass


def _evict_expired_ws_cache(now: float) -> None:
    expired = [k for k, (ts, _, _) in _WS_CACHE.items() if now - ts >= _WS_CACHE_TTL]
    for k in expired:
        _evict_ws_cache_entry(k)


def _resolve_cached(workspace_arg: str | None) -> tuple[Any, Any]:
    import time

    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    now = time.monotonic()
    _evict_expired_ws_cache(now)
    hit = _WS_CACHE.get(workspace_arg)
    if hit is not None:
        _WS_CACHE.move_to_end(workspace_arg)
        return hit[1], hit[2]
    ws = resolve_workspace(workspace_arg)
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    _WS_CACHE[workspace_arg] = (now, ws, store)
    _WS_CACHE.move_to_end(workspace_arg)
    while len(_WS_CACHE) > _WS_CACHE_MAXSIZE:
        _evict_ws_cache_entry(next(iter(_WS_CACHE)))
    return ws, store


def _dispatch(args: dict[str, Any]) -> str:
    # Heavy modules load lazily so server startup stays fast.
    from ctx.retrieval import Selector, RetrievalError, get, search, stats, charge_turn_budget, _span

    op = args.get("op")
    ws, store = _resolve_cached(args.get("workspace"))

    max_tokens = args.get("maxTokens")
    if isinstance(max_tokens, int):
        # Enforce the range the schema DECLARES. It was advertised and never
        # checked, so a negative cap flowed into the budgets below and out to
        # textutil.bounded as a negative slice (ctx.bounds). Schema and check
        # read the same constants so they cannot drift apart again.
        max_tokens = max(_MAX_TOKENS_MIN, min(_MAX_TOKENS_MAX, max_tokens))
        # Tighten budgets to the caller's cap (never loosen beyond policy),
        # on a per-call copy so the cached workspace stays pristine.
        from dataclasses import replace

        b = ws.config.budgets
        ws = replace(
            ws,
            config=replace(
                ws.config,
                budgets=replace(
                    b,
                    result_tokens=min(b.result_tokens, max_tokens),
                    digest_tokens=min(b.digest_tokens, max_tokens),
                ),
            ),
        )

    if op == "search":
        opts = args.get("options") or {}
        result = search(
            store,
            ws,
            args.get("ref") or "repo:",
            list(args.get("patterns") or []),
            fixed=bool(opts.get("fixed")),
            mode_all=bool(opts.get("all")),
            context=int(opts.get("context", 0)),
            glob=opts.get("glob"),
            scope=opts.get("scope"),
            max_matches=opts.get("maxMatches"),
        )
    elif op == "get":
        sel_raw = args.get("selector") or {}
        selector = Selector(
            lines=_span(sel_raw["lines"]) if sel_raw.get("lines") else None,
            bytes=_span(sel_raw["bytes"]) if sel_raw.get("bytes") else None,
            records=_span(sel_raw["records"]) if sel_raw.get("records") else None,
            json_pointer=sel_raw.get("jsonPointer"),
            symbol=sel_raw.get("symbol"),
            span=sel_raw.get("span"),
        )
        if not args.get("ref"):
            raise RetrievalError("get requires a ref")
        result = get(store, ws, args["ref"], selector)
    elif op == "stats":
        result = stats(store, ws, args.get("ref") or "repo:", scope=(args.get("options") or {}).get("scope"))
    elif op == "map":
        from ctx.repomap import repo_map

        opts = args.get("options") or {}
        result = repo_map(store, ws, budget=int(opts.get("budget", 600)), focus=opts.get("focus"))
    elif op in ("def", "refs", "diag"):
        from ctx.codeverbs import cmd_def, cmd_diag, cmd_refs

        opts = args.get("options") or {}
        if op == "def":
            target = opts.get("target") or args.get("ref")
            if not target:
                raise RetrievalError("def requires options.target (repo:<path>:<Symbol>)")
            result = cmd_def(store, ws, target)
        elif op == "refs":
            symbol = opts.get("symbol")
            if not symbol:
                raise RetrievalError("refs requires options.symbol")
            result = cmd_refs(store, ws, symbol, opts.get("path"))
        else:
            result = cmd_diag(store, ws, opts.get("path"))
    elif op in ("callers", "callees", "impact"):
        from ctx.callgraph import cmd_callees, cmd_callers, cmd_impact

        opts = args.get("options") or {}
        symbol = opts.get("symbol") or args.get("ref")
        if not symbol:
            raise RetrievalError(f"{op} requires options.symbol")
        if op == "callers":
            result = cmd_callers(store, ws, symbol)
        elif op == "callees":
            result = cmd_callees(store, ws, symbol)
        else:
            result = cmd_impact(store, ws, symbol, depth=int(opts.get("depth", 6)))
    elif op == "diff":
        from ctx.rundiff import run_diff

        opts = args.get("options") or {}
        ref_a = opts.get("refA")
        ref_b = opts.get("refB")
        if not ref_a or not ref_b:
            raise RetrievalError("diff requires options.refA and options.refB (run: refs)")
        result = run_diff(store, ws, ref_a, ref_b)
    elif op == "investigate":
        from ctx.plan_exec import execute_plan

        opts = args.get("options") or {}
        plan_doc = opts.get("plan")
        if not isinstance(plan_doc, dict):
            raise RetrievalError(
                "investigate requires options.plan (a ctx.plan/v1 JSON object)"
            )
        # Bounded-only by construction (SPEC §10.4): the MCP tier validates
        # at tier='mcp', so execute-class ops are typed rejections here.
        result, _code = execute_plan(ws, store, plan_doc, tier="mcp")
    elif op == "repo":
        result = stats(store, ws, "repo:")
    elif op == "doctor":
        from ctx.installer import doctor_report

        result = doctor_report(ws, antigravity=True)
    else:
        raise RetrievalError(f"unknown op {op!r}")

    warning = charge_turn_budget(store, ws, result)
    if warning:
        result = warning + "\n" + result
    return result


def _tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if name != "ctx":
        return {
            "content": [{"type": "text", "text": f"unknown tool {name!r}"}],
            "isError": True,
        }
    args = params.get("arguments") or {}
    try:
        text = _dispatch(args)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        # Same handler, same prefix as the CLI (`ctx:`, not `ctx error:`) and
        # the same exception-type attribution. No CTX_DEBUG hint in the text:
        # the traceback still goes to this server's stderr, and a hint line
        # here would be model context spent on advice the model cannot take.
        from ctx.cli import debug_enabled, format_error

        if debug_enabled():
            import traceback

            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        text = format_error(args.get("op") if isinstance(args, dict) else None, e, hint=False)
        return {"content": [{"type": "text", "text": text}], "isError": True}


def serve(bounded_only: bool = True) -> int:
    """Run the stdio MCP server until EOF. ``bounded_only`` is the only
    supported v1 mode and is accepted for forward compatibility."""
    del bounded_only
    stdin = sys.stdin.buffer
    stdout = sys.stdout

    def reply(msg_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            reply(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ctx-harness", "version": __version__},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(msg_id, {"tools": [TOOL_SCHEMA]})
        elif method == "tools/call":
            reply(msg_id, _tool_call(msg.get("params") or {}))
        elif method == "ping":
            reply(msg_id, {})
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
    return 0
