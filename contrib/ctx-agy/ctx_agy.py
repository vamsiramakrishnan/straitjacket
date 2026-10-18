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
    "Shell results name the underlying command exit status, signal and timeout. "
    "Small complete output stays inline. Large output has a deterministic digest "
    "and a run address. Use ctx_get or ctx_search to retrieve omitted evidence "
    "instead of rerunning a command merely to print more output. File tools stay "
    "native in the command-output profile. Complete the original task normally."
)


def system_instructions(*, navigation=True, composition=True):
    text = SYSTEM
    if navigation:
        text += " Use ctx_navigate for a small direct read, large file outline, or symbol body; explicit line ranges read directly."
    if composition:
        text += " ctx_query composes locate/narrow/read, e.g. refs Foo | group file | top 3 | get --context 5."
    return text


def make_tools(root: pathlib.Path, *, timeout=600.0, profile="command-output", session=None,
               result_format="compact", navigation=True, composition=True):
    """Async tools on the public, provider-independent Straitjacket runtime."""
    from ctx.sdk import ToolSession
    if profile not in {"command-output", "full"}:
        raise ValueError("unknown tool profile")
    if result_format not in {"compact", "json"}:
        raise ValueError("unknown result format")
    session = session or ToolSession(root, timeout=timeout, producer="antigravity-sdk")
    command_timeout = min(600, timeout)

    async def run_command(command: str, working_directory: str = "", timeout_seconds: float = command_timeout, operation_key: str = "") -> str:
        """Execute a command with typed status and bounded output. Working directory
        is within the workspace; retrieve omitted evidence with ctx_get/search.
        Reusing operation_key replays its original result; use a new key after edits."""
        if result_format == "compact":
            return await session.run_model(command, working_directory=working_directory, timeout_seconds=timeout_seconds, operation_key=operation_key or None)
        return (await session.run(command, working_directory=working_directory, timeout_seconds=timeout_seconds, operation_key=operation_key or None)).to_json()

    async def ctx_get(address: str, start_line: int, end_line: int) -> str:
        """Retrieve a bounded, inclusive line range from an evidence address."""
        return await session.get(address, start_line=start_line, end_line=end_line)

    async def ctx_search(address: str, pattern: str) -> str:
        """Search stored evidence or repository files for a regex, with context."""
        return await session.search(address, pattern)

    async def ctx_navigate(path: str, symbol: str = "", start_line: int = 0, end_line: int = 0, fresh: bool = False) -> str:
        """Read a small file directly, outline a large file, or read one symbol.
        Optional inclusive lines bypass outlines; fresh re-emits prior context."""
        kwargs = {"start_line": start_line, "end_line": end_line} if start_line or end_line else {}
        return await session.navigate(path, symbol=symbol or None, fresh=fresh, **kwargs)

    async def ctx_query(pipeline: str) -> str:
        """Compose observations in one bounded query: refs Foo | group file | top 3 | get --context 5."""
        return await session.query(pipeline)

    async def ctx_investigate(plan: dict) -> str:
        """Run an observe-only ctx.plan/v1 evidence plan with aggregate budgets. No shell or edits."""
        return await session.investigate(plan)

    async def ctx_repair(failure_address: str, path: str, span: str, replacement: str,
                         targeted_command: str, regression_command: str, operation_key: str = "") -> str:
        """Apply one previously observed A:B@anchor span, rerun the failing shell
        command, then a regression command. Returns exact-byte verification proof.
        This tool writes source and executes caller-supplied commands."""
        from ctx.edit_verification import Check
        if not targeted_command.strip() or not regression_command.strip():
            raise ValueError("repair needs nonempty targeted and regression commands")
        request = {"schema": "ctx.edit-request/v1", "edits": [
            {"path": path, "span": span, "replacement": replacement}]}
        return await session.repair_model(failure_address, request=request,
            targeted=[Check("behavior", ("bash", "-lc", targeted_command), command_timeout)],
            regression=[Check("behavior", ("bash", "-lc", regression_command), command_timeout)],
            operation_key=operation_key or None)

    tools = [run_command, ctx_get, ctx_search, ctx_repair]
    if navigation:
        tools.append(ctx_navigate)
    if composition:
        tools.extend([ctx_query, ctx_investigate])
    if profile == "command-output":
        return tools

    async def view_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
        """Navigate a file progressively; explicit 1-indexed ranges read directly."""
        if navigation:
            return await ctx_navigate(path, start_line=start_line, end_line=end_line)
        return await session.get(f"repo:{path}", start_line=start_line or 1, end_line=end_line or 200, fresh=True)

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

    return [*tools, view_file, search_dir, list_dir, find_file]


async def run(prompt: str, root: pathlib.Path, model: str, timeout: float,
              contain: bool, profile="command-output", usage_semantics="unknown", evidence_output=None,
              result_format="compact", navigation=True, composition=True, task_id=None, context_epoch=None) -> dict:
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
    from google.antigravity.hooks import policy
    from google.antigravity.types import BuiltinTools
    from ctx.sdk import ToolSession
    from ctx.tooltelemetry import usage_record
    from ctx.task_runtime import ExecutionLimits

    limits = ExecutionLimits(wall_seconds=timeout, call_seconds=min(600, timeout), max_steps=4096, capture_bytes=8*1024*1024)
    session = ToolSession(root, timeout=timeout, producer="antigravity-sdk", durable=True,
        task_id=task_id, limits=None if task_id else limits, context_epoch=context_epoch) if contain else None
    tools = make_tools(root, timeout=timeout, profile=profile, session=session,
        result_format=result_format, navigation=navigation, composition=composition) if contain else []
    names = FLOODING_BUILTINS if profile == "full" else ("RUN_COMMAND",)
    disabled = [getattr(BuiltinTools, name) for name in names] if contain else []
    cfg = LocalAgentConfig(
        model=model,
        api_key=os.environ["GEMINI_API_KEY"],
        workspaces=[str(root)],
        tools=tools,
        capabilities=CapabilitiesConfig(disabled_tools=disabled),
        policies=[policy.allow_all()],
        system_instructions=system_instructions(navigation=navigation, composition=composition) if contain else None,
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
        "task_id": session.task_id if session else None,
        "model_requests": None,  # Aggregate usage does not expose request boundaries.
        **(session.metrics if session else {"custom_tool_calls": 0, "delivered_bytes": 0, "retrieval_calls": 0}),
        "all_tool_calls": None,  # Native edits/other builtins are not intercepted.
        "delivery": {"format": result_format, "navigation": navigation, "composition": composition},
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
    ap.add_argument("--result-format", choices=("compact", "json"), default="compact")
    ap.add_argument("--no-navigation", action="store_true")
    ap.add_argument("--no-composition", action="store_true")
    ap.add_argument("--task-id", help="resume a durable tool task (operation keys are required for exact replay)")
    ap.add_argument("--context-epoch", help="enable read reuse; advance this value after any model context loss")
    ap.add_argument("--usage-input-semantics", choices=("unknown", "includes_cache", "excludes_cache"), default="unknown")
    ap.add_argument("--evidence-output", type=pathlib.Path, help="export observed evidence as a portable capsule")
    ap.add_argument("--json", action="store_true", help="print the usage record too")
    ns = ap.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        print("error: GEMINI_API_KEY not set", file=sys.stderr)
        return 2
    root = pathlib.Path(ns.add_dir).resolve()
    result = asyncio.run(run(ns.prompt, root, ns.model, ns.timeout, not ns.no_contain,
        profile=ns.tool_profile, usage_semantics=ns.usage_input_semantics, evidence_output=ns.evidence_output,
        result_format=ns.result_format, navigation=not ns.no_navigation, composition=not ns.no_composition,
        task_id=ns.task_id, context_epoch=ns.context_epoch))
    print(result["text"])
    if ns.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text"}))
    return 1 if (result["error"] or result["timed_out"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
