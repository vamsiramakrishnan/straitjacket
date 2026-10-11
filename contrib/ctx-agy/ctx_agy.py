#!/usr/bin/env python3
"""`ctx agy` — a headless, harnessed Antigravity CLI built on the Agent SDK.

Why this exists
---------------
The official ``agy`` binary cannot be harnessed the way Claude Code and Codex
can, for two independent reasons:

1. **It cannot be driven headlessly.** It authenticates by interactive OAuth
   browser login and ignores ``GEMINI_API_KEY``, so no CI job, cron, or
   orchestrator node can launch it.
2. **Its hook contract cannot contain anything after the fact.** Per
   https://antigravity.google/docs/hooks, PreToolUse has no field for modified
   arguments and PostToolUse's only legal output is ``{}``. So on that host the
   birth gate can only *deny*, and there is no output-side safety net at all
   (see ADR 005).

This shim sidesteps both by using the ``google-antigravity`` **Agent SDK**,
which authenticates with ``GEMINI_API_KEY`` and — crucially — lets the embedder
**own the tools**. Containment stops being something applied *to* a tool result
and becomes a property of the tool itself: the flooding builtins are disabled
and replaced with ``ctx``-backed equivalents, so an oversized result is bounded
*before it is ever returned to the model*. No substitution API is required,
which is exactly the capability the published hook contract withholds.

The SDK's own hooks do not help here and are deliberately not used for
containment: ``PostToolCallHook`` is an ``InspectHook``, so like the published
PostToolUse contract it can observe a result but not replace it. The tool
boundary is the only place where substitution is actually possible, so that is
where this shim does the work.

What is replaced
----------------
The command-output profile replaces RUN_COMMAND and exposes ctx_get/ctx_search.
File reading, search and editing stay native. --tool-profile full also replaces
VIEW_FILE, SEARCH_DIR, LIST_DIR and FIND_FILE through the same public ToolSession.
All tools are async; command termination is typed and cancellation belongs to
shared capture. No private manifest directory or printed-header parsing.

Install
-------
The SDK conflicts with the system PyJWT, so it lives in its own venv::

    python -m venv /tmp/agy-venv
    /tmp/agy-venv/bin/pip install google-antigravity ctx-harness
    GEMINI_API_KEY=... /tmp/agy-venv/bin/python contrib/ctx-agy/ctx_agy.py \
        -p "fix the failing test" --model gemini-3.6-flash

Flag names mirror the real CLI (``-p/--print``, ``--model``) so it can stand in
where ``agy`` would be launched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

# Provider imports are inside run(); local tool tests need no vendor SDK.
FLOODING_BUILTINS = ("RUN_COMMAND", "VIEW_FILE", "SEARCH_DIR", "LIST_DIR", "FIND_FILE")

SYSTEM = (
    "Shell commands return a structured result. command_exit_code is the "
    "underlying command status; signal and timed_out describe other termination. "
    "Small complete output stays inline. Large output has a deterministic digest "
    "and a run address. Use ctx_get or ctx_search to retrieve omitted evidence "
    "instead of rerunning a command merely to print more output. File tools stay "
    "native in the command-output profile. Complete the original task normally."
)


def make_tools(root: pathlib.Path, *, timeout=600.0, profile="command-output", session=None):
    """Async tools on the public, provider-independent Straitjacket runtime."""
    from ctx.sdk import ToolSession
    if profile not in {"command-output", "full"}:
        raise ValueError("unknown tool profile")
    session = session or ToolSession(root, timeout=timeout, producer="antigravity-sdk")
    command_timeout = min(600, timeout)

    async def run_command(command: str, working_directory: str = "", timeout_seconds: float = command_timeout) -> str:
        """Execute a command with typed status and bounded output. Working directory
        is within the workspace; retrieve omitted evidence with ctx_get/search."""
        result = await session.run(command, working_directory=working_directory, timeout_seconds=timeout_seconds)
        return result.to_json()

    async def ctx_get(address: str, start_line: int, end_line: int) -> str:
        """Retrieve a bounded, inclusive line range from an evidence address."""
        return await session.get(address, start_line=start_line, end_line=end_line)

    async def ctx_search(address: str, pattern: str) -> str:
        """Search stored evidence or repository files for a regex, with context."""
        return await session.search(address, pattern)

    if profile == "command-output":
        return [run_command, ctx_get, ctx_search]

    async def view_file(path: str, start_line: int = 1, end_line: int = 200) -> str:
        """Read a bounded, 1-indexed file span; omitted evidence remains addressable."""
        return await session.get(f"repo:{path}", start_line=start_line, end_line=end_line)

    async def search_dir(pattern: str, path: str = "") -> str:
        """Search repository files with a bounded hit census."""
        return await session.search(f"repo:{path}", pattern)

    async def list_dir(path: str = "") -> str:
        """Summarise a directory's file counts, languages and size."""
        return await session.retrieve("stats", ref=f"repo:{path}")

    async def find_file(name: str) -> str:
        """List files matching a path glob, including empty files."""
        import shlex
        return await session.query(f"corpus --glob {shlex.quote(name)}")

    async def ctx_query(pipeline: str) -> str:
        """Compose observation in one bounded query, e.g. refs Foo | group file | top 3."""
        return await session.query(pipeline)

    return [run_command, ctx_get, ctx_search, view_file, search_dir, list_dir, find_file, ctx_query]


async def run(prompt: str, root: pathlib.Path, model: str, timeout: float,
              contain: bool, profile="command-output", usage_semantics="unknown", evidence_output=None) -> dict:
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
    from google.antigravity.hooks import policy
    from google.antigravity.types import BuiltinTools
    from ctx.sdk import ToolSession
    from ctx.tooltelemetry import usage_record

    session = ToolSession(root, timeout=timeout, producer="antigravity-sdk") if contain else None
    tools = make_tools(root, timeout=timeout, profile=profile, session=session) if contain else []
    names = FLOODING_BUILTINS if profile == "full" else ("RUN_COMMAND",)
    disabled = [getattr(BuiltinTools, name) for name in names] if contain else []
    cfg = LocalAgentConfig(
        model=model,
        api_key=os.environ["GEMINI_API_KEY"],
        workspaces=[str(root)],
        tools=tools,
        capabilities=CapabilitiesConfig(disabled_tools=disabled),
        policies=[policy.allow_all()],
        system_instructions=SYSTEM if contain else None,
        app_data_dir=str(root / ".agdata"),
    )
    text, timed_out, error = "", False, ""
    async with Agent(cfg) as agent:
        try:
            resp = await asyncio.wait_for(agent.chat(prompt), timeout=timeout)
            text = await resp.text()
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"[:300]
        u = agent.conversation.total_usage
    usage = usage_record(u, input_semantics=usage_semantics)
    if evidence_output is not None and session is not None and session.handles:
        await session.export(evidence_output)
    return {
        "text": text,
        "input_tokens": usage["raw"]["prompt_token_count"],
        "output_tokens": usage["raw"]["candidates_token_count"],
        "total_tokens": usage["raw"]["total_token_count"],
        "usage": usage,
        "timed_out": timed_out,
        "error": error,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ctx agy",
        description="Headless, harnessed Antigravity agent (google-antigravity SDK)",
    )
    ap.add_argument("-p", "--print", dest="prompt", required=True,
                    help="run a single prompt non-interactively and print the response")
    ap.add_argument("--model", default="gemini-3.6-flash")
    ap.add_argument("--add-dir", default=".", help="workspace root")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--no-contain", action="store_true",
                    help="run with the native builtins instead of the ctx-backed "
                         "tools (the uncontained baseline, for A/B only)")
    ap.add_argument("--tool-profile", choices=("command-output", "full"), default="command-output")
    ap.add_argument("--usage-input-semantics", choices=("unknown", "includes_cache", "excludes_cache"), default="unknown")
    ap.add_argument("--evidence-output", type=pathlib.Path, help="export observed evidence as a portable capsule")
    ap.add_argument("--json", action="store_true", help="print the usage record too")
    ns = ap.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        print("error: GEMINI_API_KEY not set", file=sys.stderr)
        return 2
    root = pathlib.Path(ns.add_dir).resolve()
    result = asyncio.run(run(ns.prompt, root, ns.model, ns.timeout, not ns.no_contain,
        profile=ns.tool_profile, usage_semantics=ns.usage_input_semantics, evidence_output=ns.evidence_output))
    print(result["text"])
    if ns.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text"}))
    return 1 if (result["error"] or result["timed_out"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
