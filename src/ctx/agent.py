"""``ctx agent`` — ctx as a runtime: the Claude Agent SDK harness with the
harness's own retrieval tools, its hooks, and a context pack at turn one.

`ctx wrap claude` keeps the host and shapes what flows through it. This is
the other end of the same idea: ctx *is* the host. It owns the tool surface
(a lean built-in set plus in-process ``ctx`` tools), the system prompt, the
first turn (the task and a `ctx pack` of where to look), and the same
PreToolUse/PostToolUse containment the wrapper installs — with no proxy, no
prefix tax, and no native Grep/Glob for the model to reach for first.

What it does not do: call the Messages API itself. The Agent SDK drives the
Claude Code binary (the same one `claude -p` runs), so every session is
billed and cached the way the host does it, transcripts land where they
always do, and the account's login is the one the host already holds. That
is the deliberate trade: the tool surface and the turn structure are ctx's,
the loop and the wire are the host's.

Print-mode result JSON is the host's shape (``num_turns``, ``usage``,
``total_cost_usd``, ``modelUsage``), so the agentbench harness reads a
``ctx agent`` session exactly like a ``claude -p`` one.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

#: Built-in tools the agent keeps. Search and navigation come from ctx.
BUILTIN_TOOLS = ("Bash", "Read", "Edit", "Write", "MultiEdit")
MCP_SERVER = "ctx"

_TOOL_GUIDE = (
    "Repository search and navigation are the ctx tools (mcp__ctx__*): "
    "`search` (regex with filters file:/!file:/lang:/sym:/case:no; type:commit or type:diff "
    "searches git history), `outline` (a file's symbols with line ranges — read a range or a "
    "symbol next, never the whole file), `get` (a bounded slice by lines or by symbol), "
    "`refs` (where a name is used), `pack` (where to look first for a task). "
    "Results carry handles (run:/blob:/snapshot:) you can open with `get`. "
    "Use `outline` then `get` by symbol instead of reading whole files. "
    "Do not run grep, rg, ag or git grep in Bash: the harness refuses them and answers with "
    "the equivalent `search` call; searching goes through the index."
)

#: Shell search commands the router refuses in favour of the `search` tool.
_SEARCH_HEADS = ("grep", "rg", "ag", "egrep", "fgrep")


def _search_equivalent(command: str) -> str | None:
    """The `search` tool call that answers a shell grep, or None when the
    command is not a search. Understands the common spellings: flags, a
    ``-e`` pattern, ``-i``, ``--include=GLOB``, paths, ``git grep``, and a
    leading ``cd DIR &&``."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    # `cd x && grep ...` / `grep ... | head`: the search is the first segment
    # whose head is a grep; everything after a pipe is the model's own filter.
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in ("&&", "||", ";", "|"):
            segments.append([])
        else:
            segments[-1].append(tok)
    seg = None
    for cand in segments:
        if cand and (cand[0] in _SEARCH_HEADS or (cand[0] == "git" and len(cand) > 1 and cand[1] == "grep")):
            seg = cand
            break
    if seg is None:
        return None
    args = seg[2:] if seg[0] == "git" else seg[1:]
    pattern: str | None = None
    paths: list[str] = []
    filters: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-e" and i + 1 < len(args):
            pattern = args[i + 1]
            i += 2
            continue
        if a.startswith("--include="):
            filters.append(f"file:{a.split('=', 1)[1]}")
        elif a == "--include" and i + 1 < len(args):
            filters.append(f"file:{args[i + 1]}")
            i += 1
        elif a.startswith("-") and a != "-":
            if "i" in a.lstrip("-") and not a.startswith("--"):
                filters.append("case:no")
        elif pattern is None:
            pattern = a
        else:
            paths.append(a)
        i += 1
    if not pattern:
        return None
    filters += [f"file:{p.rstrip('/')}" for p in paths if p not in (".", "./")]
    query = " ".join([shlex.quote(pattern) if " " in pattern else pattern, *dict.fromkeys(filters)])
    return f'search {{"query": "{query}"}}'


async def _route_bash(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
    """PreToolUse router: a shell grep is refused and the equivalent `search`
    call is handed back as the reason, so the model's next call goes through
    the index (docs/CODE-SEARCH.md). Everything else passes to the ctx hook
    installed through the settings file."""
    tool_input = input_data.get("tool_input") or {}
    command = str(tool_input.get("command") or "")
    equivalent = _search_equivalent(command)
    if equivalent is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "CTX_ROUTE: shell grep is not used here; the repository is indexed. "
                f"Call the search tool instead: {equivalent}. Filters: file:PATH !file:PATH "
                "lang:python case:no sym:NAME; type:commit / type:diff search history."
            ),
        }
    }


class AgentUnavailable(RuntimeError):
    pass


def _sdk():
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as e:
        raise AgentUnavailable(
            "the Claude Agent SDK is not installed; pip install 'ctx-harness[agent]' "
            "(in an environment where the host's `claude` binary is on PATH)"
        ) from e
    return claude_agent_sdk


# ------------------------------------------------------------ ctx tools
def _tools(ws, store):
    """In-process MCP tools over the retrieval verbs, each bounded the way
    the CLI verb is (the same renderer, the same budgets)."""
    sdk = _sdk()

    def text(out: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": out}]}

    @sdk.tool(
        "search",
        "Search the repository. query = patterns and filters as on a command line, e.g. "
        "\"TokenBucket file:src/ !file:tests\", \"'rate limit' case:no\", \"sym:resolve_refs\", "
        "\"type:commit prefix\" (git history), \"type:diff ENABLE_TOOL_SEARCH\" (what changed).",
        {"query": str},
    )
    async def search(args: dict[str, Any]) -> dict[str, Any]:
        from ctx.retrieval import RetrievalError, search as _search

        try:
            tokens = shlex.split(str(args.get("query", "")))
            if not tokens:
                return text("search: give a pattern")
            return text(_search(store, ws, "repo:", tokens))
        except (RetrievalError, ValueError) as e:
            return text(f"search: {e}")

    @sdk.tool(
        "outline",
        "A file's symbols (kind, name, line range) so the next read is a range or a symbol. "
        "path is repo-relative.",
        {"path": str},
    )
    async def outline(args: dict[str, Any]) -> dict[str, Any]:
        from ctx.retrieval import RetrievalError, stats

        try:
            return text(stats(store, ws, f"repo:{str(args.get('path', '')).lstrip('/')}"))
        except (RetrievalError, ValueError, OSError) as e:
            return text(f"outline: {e}")

    @sdk.tool(
        "get",
        "A bounded slice of a file: lines 'A:B', or a symbol name (any language with a "
        "parser), or a handle such as run:<id>#stdout or blob:<id>. path is repo-relative "
        "or a handle.",
        {"path": str, "lines": str, "symbol": str},
    )
    async def get(args: dict[str, Any]) -> dict[str, Any]:
        from ctx.retrieval import RetrievalError, Selector, _span_anchored, get as _get

        path = str(args.get("path", "")).strip()
        ref = path if ":" in path.split("/")[0] and not path.startswith("/") else f"repo:{path.lstrip('/')}"
        lines = str(args.get("lines") or "").strip() or None
        symbol = str(args.get("symbol") or "").strip() or None
        try:
            la, lb, anchor = _span_anchored(lines) if lines else (None, None, None)
            sel = Selector(lines=(la, lb) if la is not None else None, lines_anchor=anchor, symbol=symbol)
            return text(_get(store, ws, ref, sel))
        except (RetrievalError, ValueError, OSError) as e:
            return text(f"get: {e}")

    @sdk.tool(
        "refs",
        "Where a name is used across the repository (exact index, language server, or "
        "textual — the answer says which). symbol is a name or Class.method.",
        {"symbol": str},
    )
    async def refs(args: dict[str, Any]) -> dict[str, Any]:
        from ctx.codeverbs import cmd_refs

        try:
            return text(cmd_refs(store, ws, str(args.get("symbol", "")).strip(), None))
        except Exception as e:  # the verb's own errors are already text
            return text(f"refs: {e}")

    @sdk.tool(
        "pack",
        "Where to look first for a task: files ranked by task terms, symbols and history, "
        "with each file's outline and the reason it is there.",
        {"task": str},
    )
    async def pack(args: dict[str, Any]) -> dict[str, Any]:
        from ctx.pack import build_pack, render_pack

        try:
            return text(render_pack(build_pack(store, ws, str(args.get("task", "")))))
        except Exception as e:
            return text(f"pack: {e}")

    return [search, outline, get, refs, pack]


# ------------------------------------------------------------- the run
def _hook_settings_file(ctx_exe: str) -> Path:
    from ctx.installer import claude_hook_settings

    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="ctx-agent-", delete=False)
    json.dump(claude_hook_settings(ctx_exe), tmp)
    tmp.close()
    return Path(tmp.name)


def _system_prompt_append() -> str:
    from ctx.wrap import _OUTPUT_DISCIPLINE, _SINGLE_SHOT_NOTICE

    return f"{_OUTPUT_DISCIPLINE} {_SINGLE_SHOT_NOTICE} {_TOOL_GUIDE}"


def _first_turn(ws, store, task: str, *, pack: bool, budget: int) -> tuple[str, dict[str, Any] | None]:
    if not pack:
        return task, None
    from ctx.pack import build_pack, render_pack

    try:
        p = build_pack(store, ws, task, budget_tokens=budget)
    except Exception as e:  # a pack failure never costs the session
        return task, {"error": f"{type(e).__name__}: {e}"}
    rendered = render_pack(p)
    prompt = (
        f"{task}\n\n<context-pack>\n{rendered}\n</context-pack>\n"
        "The context pack above ranks where to look first, with the reason for each "
        "entry; open a listed file by symbol or range rather than whole."
    )
    return prompt, {"files": [r.rel for r in p.rows], "tokens": len(rendered) // 4}


def _result_json(msg, *, pack_info, wall: float) -> dict[str, Any]:
    usage = dict(msg.usage or {})
    model_usage = {}
    for name, mu in (msg.model_usage or {}).items():
        try:
            model_usage[name] = dict(mu) if isinstance(mu, dict) else {
                k: getattr(mu, k) for k in ("input_tokens", "output_tokens",
                                             "cache_read_input_tokens", "cache_creation_input_tokens",
                                             "cost_usd") if hasattr(mu, k)
            }
        except Exception:
            model_usage[name] = {}
    return {
        "type": "result",
        "subtype": msg.subtype,
        "is_error": bool(msg.is_error),
        "num_turns": msg.num_turns,
        "duration_ms": msg.duration_ms,
        "duration_api_ms": msg.duration_api_ms,
        "total_cost_usd": msg.total_cost_usd,
        "usage": usage,
        "modelUsage": model_usage,
        "session_id": msg.session_id,
        "result": msg.result,
        "stop_reason": getattr(msg, "stop_reason", None),
        "errors": list(msg.errors or []),
        "runtime": "ctx agent",
        "pack": pack_info,
        "wall_s": round(wall, 1),
    }


async def _run(ws, store, task: str, *, model: str | None, max_turns: int | None,
               pack: bool, pack_budget: int, output_format: str, ctx_exe: str,
               claude_path: str | None, stream) -> dict[str, Any]:
    sdk = _sdk()
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage

    prompt, pack_info = _first_turn(ws, store, task, pack=pack, budget=pack_budget)
    settings = _hook_settings_file(ctx_exe)
    tools = _tools(ws, store)
    server = sdk.create_sdk_mcp_server(name=MCP_SERVER, version="1.0.0", tools=tools)
    env = {}
    # Print mode kills background subagents at a fixed ceiling; the wrapper
    # raises it for the same reason (see ctx.wrap._with_print_bg_wait_ceiling).
    env.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "3600000"))
    # Pre-approved tools rather than bypassPermissions: the same posture as
    # `claude -p --allowedTools`, and the only one the host accepts as root.
    allowed = [*BUILTIN_TOOLS, *(f"mcp__{MCP_SERVER}__{t.name}" for t in tools)]
    options = ClaudeAgentOptions(
        tools=list(BUILTIN_TOOLS),
        allowed_tools=allowed,
        system_prompt={"type": "preset", "preset": "claude_code", "append": _system_prompt_append()},
        mcp_servers={MCP_SERVER: server},
        strict_mcp_config=True,
        permission_mode="acceptEdits",
        max_turns=max_turns,
        model=model,
        cwd=str(ws.root),
        settings=str(settings),
        cli_path=claude_path,
        env=env,
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_route_bash], timeout=5)]},
    )
    from claude_agent_sdk import ProcessError

    t0 = time.monotonic()
    result: dict[str, Any] | None = None
    try:
        try:
            async for msg in sdk.query(prompt=prompt, options=options):
                if isinstance(msg, ResultMessage):
                    result = _result_json(msg, pack_info=pack_info, wall=time.monotonic() - t0)
                elif stream is not None:
                    stream(msg)
        except ProcessError:
            # The CLI ends a failed run (max turns, an API error) by emitting
            # its result message and exiting non-zero; the SDK yields the
            # message and then raises. The result is the record — the same
            # one `claude -p` prints before its own exit 1 — so it is kept and
            # the exit status carries the failure. Without a result there is
            # nothing to report, and the error stands.
            if result is None:
                raise
    finally:
        try:
            settings.unlink()
        except OSError:
            pass
    if result is None:
        result = {"type": "result", "subtype": "error_no_result", "is_error": True, "num_turns": 0,
                  "usage": {}, "total_cost_usd": 0.0, "runtime": "ctx agent", "pack": pack_info,
                  "wall_s": round(time.monotonic() - t0, 1)}
    return result


def run_agent(ws, task: str, *, model: str | None = None, max_turns: int | None = None,
              pack: bool = True, pack_budget: int = 2500, output_format: str = "text",
              verbose: bool = False) -> int:
    """Run one print-mode session; print the result in ``output_format``
    (``json`` is the host's result shape, ``text`` is the final answer)."""
    import asyncio

    from ctx.store import Store

    try:
        _sdk()
    except AgentUnavailable as e:
        print(f"ctx agent: {e}", file=sys.stderr)
        return 2
    ctx_exe = shutil.which("ctx") or sys.executable + " -m ctx"
    claude_path = shutil.which("claude")
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)

    def stream(msg) -> None:
        if not verbose:
            return
        kind = type(msg).__name__
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for block in content:
                if hasattr(block, "text"):
                    print(f"[{kind}] {block.text[:400]}", file=sys.stderr)
                elif hasattr(block, "name"):
                    print(f"[{kind}] tool {block.name} {json.dumps(getattr(block, 'input', {}))[:300]}",
                          file=sys.stderr)

    try:
        result = asyncio.run(_run(
            ws, store, task, model=model, max_turns=max_turns, pack=pack, pack_budget=pack_budget,
            output_format=output_format, ctx_exe=ctx_exe, claude_path=claude_path, stream=stream,
        ))
    finally:
        store.close()
    if output_format == "json":
        print(json.dumps(result, sort_keys=True))
    else:
        print(result.get("result") or "")
        if result.get("is_error"):
            print(f"ctx agent: {result.get('subtype')} {' · '.join(result.get('errors') or [])}",
                  file=sys.stderr)
    return 1 if result.get("is_error") else 0
