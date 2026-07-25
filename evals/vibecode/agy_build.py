"""Antigravity SDK builder — runs an Antigravity (Gemini) agent with real file
+ shell tools to build an app in a directory. Invoked as a subprocess by
harness.py so the SDK can live in its own venv (it conflicts with the system
PyJWT). Prints one JSON line of usage to stdout.

Usage (inside the SDK venv):
  GEMINI_API_KEY=... <agy-venv>/bin/python agy_build.py \
      --dir <build_dir> --model gemini-3.5-flash --prompt-file <f> --timeout 600
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

SYSTEM = (
    "You are an autonomous coding agent building a web app. Use your file tools "
    "to write files and the `shell` tool to run commands, chmod, and check things. "
    "Keep going until the app is complete and ./start.sh exists and is executable. "
    "Do not ask questions; make reasonable choices and finish."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--contain", action="store_true",
                    help="route shell output through `ctx run` (straitjacket) and "
                         "expose a `ctx_query` tool for retrieval by address")
    ns = ap.parse_args()
    root = pathlib.Path(ns.dir).resolve()
    task = pathlib.Path(ns.prompt_file).read_text()

    def shell(command: str) -> str:
        """Execute a shell command in the workspace; returns stdout+stderr."""
        p = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=str(root))
        return (p.stdout + p.stderr)[:6000]

    def contained_shell(command: str) -> str:
        """Execute a shell command in the workspace; returns stdout+stderr."""
        p = subprocess.run(["ctx", "run", "--", "bash", "-lc", command],
                           capture_output=True, text=True, cwd=str(root))
        return (p.stdout + p.stderr)[:6000]

    def ctx_query(args: str) -> str:
        """Run a bounded `ctx` retrieval command (e.g. 'get run:<id>#stdout
        --lines 100:140' or 'search run:<id> Error'). Use the run id and address
        printed by the `shell` digest to pull the exact omitted lines."""
        p = subprocess.run(["ctx", *args.split()], capture_output=True, text=True,
                           cwd=str(root))
        return (p.stdout + p.stderr)[:6000]

    tools = [contained_shell, ctx_query] if ns.contain else [shell]
    if ns.contain:
        contained_shell.__name__ = "shell"

    async def run() -> dict:
        cfg = LocalAgentConfig(
            model=ns.model,
            api_key=os.environ["GEMINI_API_KEY"],
            workspaces=[str(root)],
            tools=tools,
            capabilities=CapabilitiesConfig(disabled_tools=[BuiltinTools.RUN_COMMAND]),
            policies=[policy.allow_all()],
            system_instructions=SYSTEM,
            app_data_dir=str(root / ".agdata"),
        )
        text, timed_out = "", False
        async with Agent(cfg) as agent:
            try:
                resp = await asyncio.wait_for(agent.chat(task), timeout=ns.timeout)
                text = await resp.text()
            except asyncio.TimeoutError:
                timed_out = True
            u = agent.conversation.total_usage
        return {
            "input": u.prompt_token_count or 0,
            "output": u.candidates_token_count or 0,
            "total": u.total_token_count or 0,
            "timed_out": timed_out,
            "final_text": text[:400],
        }

    result = asyncio.run(run())
    # make start.sh executable if the agent forgot
    s = root / "start.sh"
    if s.exists():
        s.chmod(0o755)
    print(json.dumps(result))
    return 0 if not result["timed_out"] else 1


if __name__ == "__main__":
    sys.exit(main())
