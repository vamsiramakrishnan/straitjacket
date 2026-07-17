"""Bounded MCP retrieval server (SPEC §10.4).

Exposes exactly one stable tool schema with an ``op`` discriminator:
``search | get | stats | map | repo | doctor``. Arbitrary command execution stays
on ``ctx run`` through the native command tool so the user's permission flow
remains visible; this server is bounded-only by construction.

Transport: MCP stdio — newline-delimited JSON-RPC 2.0.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ctx import __version__

PROTOCOL_VERSION = "2025-06-18"

TOOL_SCHEMA: dict[str, Any] = {
    "name": "ctx",
    "description": (
        "Execute bounded retrieval against repository state or captured artifacts "
        "without placing unbounded output in model context. Ops: search (multi-pattern "
        "over run:/blob:/repo: refs), get (exact line/byte/record/json-pointer slices), "
        "stats (schema and repository shape), map (ranked budget-fitted codebase map), "
        "repo (workspace summary), doctor (health)."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["op"],
        "properties": {
            "op": {"enum": ["search", "get", "stats", "map", "repo", "doctor"]},
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
                "description": "search options: {fixed,all,context,glob,scope,maxMatches} · map options: {budget,focus}",
            },
            "maxTokens": {"type": "integer", "minimum": 64, "maximum": 4000},
        },
        "additionalProperties": False,
    },
}


# Long-lived server: cache workspace resolution + store connections so each
# tool call avoids re-spawning git subprocesses and reopening SQLite.
_WS_CACHE: dict[str | None, tuple[float, Any, Any]] = {}
_WS_CACHE_TTL = 10.0


def _resolve_cached(workspace_arg: str | None) -> tuple[Any, Any]:
    import time

    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    now = time.monotonic()
    hit = _WS_CACHE.get(workspace_arg)
    if hit and now - hit[0] < _WS_CACHE_TTL:
        return hit[1], hit[2]
    ws = resolve_workspace(workspace_arg)
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    _WS_CACHE[workspace_arg] = (now, ws, store)
    return ws, store


def _dispatch(args: dict[str, Any]) -> str:
    # Heavy modules load lazily so server startup stays fast.
    from ctx.retrieval import Selector, RetrievalError, get, search, stats, charge_turn_budget, _span

    op = args.get("op")
    ws, store = _resolve_cached(args.get("workspace"))

    max_tokens = args.get("maxTokens")
    if isinstance(max_tokens, int):
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
        return {"content": [{"type": "text", "text": f"ctx error: {e}"}], "isError": True}


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
