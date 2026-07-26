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
Disabled builtins -> ctx-backed tools returning bounded, addressed output:

  RUN_COMMAND       -> run_command   (ctx run: digest + `ctx get run:<id>` refs)
  VIEW_FILE         -> view_file     (ctx get repo:<path>, bounded by line span)
  SEARCH_DIR        -> search_dir    (ctx search: bounded hit census)
  LIST_DIR          -> list_dir      (ctx stats: bounded tree summary)
  FIND_FILE         -> find_file     (ctx search over paths)

Left native (they do not flood): CREATE_FILE, EDIT_FILE, ASK_QUESTION, FINISH.
Also exposed: ``ctx_query`` so the agent can resolve any omitted bytes by the
address a digest printed.

Install
-------
The SDK conflicts with the system PyJWT, so it lives in its own venv::

    python -m venv /tmp/agy-venv
    /tmp/agy-venv/bin/pip install google-antigravity
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
import subprocess
import sys

from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
from google.antigravity.hooks import policy
from google.antigravity.types import BuiltinTools

# Every builtin that can put unbounded bytes into the transcript. Writes and
# control-flow builtins are left alone: they do not flood, and replacing them
# would only add failure modes.
FLOODING_BUILTINS = [
    BuiltinTools.RUN_COMMAND,
    BuiltinTools.VIEW_FILE,
    BuiltinTools.SEARCH_DIR,
    BuiltinTools.LIST_DIR,
    BuiltinTools.FIND_FILE,
]

SYSTEM = (
    "You are an autonomous coding agent working in a real repository. Your "
    "shell, file-view and search tools return a BOUNDED DIGEST, not raw output: "
    "the full bytes are stored and every omitted span keeps an address. When a "
    "digest is not enough, call `ctx_query` with the address it printed (e.g. "
    "'get run:<id>#stdout --lines 1200:1260' or 'search run:<id> Error') to pull "
    "the exact original bytes. Do not try to defeat the digest by re-running a "
    "command to see more of it — retrieve by address instead. Work step by step "
    "and keep going until the task is complete."
)

MAX_TOOL_CHARS = 200_000  # guardrail: one pathological result cannot 400 the API


def _ctx(root: pathlib.Path, *args: str) -> str:
    """Run a ctx verb in the workspace and return its bounded output."""
    p = subprocess.run(["ctx", *args], capture_output=True, text=True, cwd=str(root))
    return ((p.stdout or "") + (p.stderr or ""))[:MAX_TOOL_CHARS]


def make_tools(root: pathlib.Path):
    """The ctx-backed replacements for the disabled builtins.

    Each one is a real tool the model sees; the containment happens inside it,
    so there is nothing to substitute afterwards and nothing that can leak past
    a hook that is only allowed to inspect.
    """

    def run_command(command: str) -> str:
        """Execute a shell command in the workspace. Returns a bounded digest of
        the output; use ctx_query with the printed address for omitted bytes."""
        return _ctx(root, "run", "--", "bash", "-lc", command)

    def view_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
        """Read a file. Returns bounded content; large files are summarised with
        a retrieval address. Optionally pass a 1-indexed line span."""
        args = ["get", f"repo:{path}"]
        if start_line or end_line:
            args += ["--lines", f"{start_line or 1}:{end_line or (start_line or 1) + 200}"]
        return _ctx(root, *args)

    def search_dir(pattern: str, path: str = "") -> str:
        """Search the repository for a regex. Returns a bounded hit census with
        addresses rather than every matching line."""
        return _ctx(root, "search", f"repo:{path}", pattern)

    def list_dir(path: str = "") -> str:
        """Summarise a directory: file count, size, languages, largest files."""
        return _ctx(root, "stats", f"repo:{path}")

    def find_file(name: str) -> str:
        """Find files whose path matches a glob, e.g. '*.py'. Returns a bounded
        listing (the search census names the files that matched)."""
        return _ctx(root, "search", "repo:", ".", "--glob", name, "--max-matches", "1")

    def ctx_query(args: str) -> str:
        """Resolve omitted bytes by address. Pass a ctx retrieval command, e.g.
        'get run:8d8335db6848#stdout --lines 1284:1300' or
        'search run:8d8335db6848 Traceback'."""
        return _ctx(root, *args.split())

    return [run_command, view_file, search_dir, list_dir, find_file, ctx_query]


async def run(prompt: str, root: pathlib.Path, model: str, timeout: float,
              contain: bool) -> dict:
    tools = make_tools(root) if contain else []
    disabled = FLOODING_BUILTINS if contain else []
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
    return {
        "text": text,
        "input_tokens": u.prompt_token_count or 0,
        "output_tokens": u.candidates_token_count or 0,
        "total_tokens": u.total_token_count or 0,
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
    ap.add_argument("--json", action="store_true", help="print the usage record too")
    ns = ap.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        print("error: GEMINI_API_KEY not set", file=sys.stderr)
        return 2
    root = pathlib.Path(ns.add_dir).resolve()
    result = asyncio.run(run(ns.prompt, root, ns.model, ns.timeout, not ns.no_contain))
    print(result["text"])
    if ns.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text"}))
    return 1 if (result["error"] or result["timed_out"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
