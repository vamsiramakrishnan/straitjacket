# Straitjacket managed native hooks v1
"""Hermes plugin: active only where ctx.toml is present above the working directory."""
import json
import os
import subprocess
from pathlib import Path

CTX_ARGV = __CTX_ARGV__


def _bridge(stage, tool_name, args, task_id="", result=None, **kwargs):
    cwd = Path.cwd()
    if not any((p / "ctx.toml").is_file() for p in (cwd, *cwd.parents)):
        return {}
    payload = {"cwd": str(cwd), "tool_name": tool_name, "tool_input": args,
               "session_id": kwargs.get("session_id") or task_id,
               "tool_use_id": kwargs.get("tool_call_id", ""),
               "is_error": kwargs.get("status") == "error"}
    if result is not None:
        payload["tool_response"] = result
    try:
        proc = subprocess.run([*CTX_ARGV, "hook", "hermes", stage], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=15, cwd=cwd)
        if proc.returncode:
            raise ValueError("hook process failed")
        return json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ({"action": "block", "reason": "Straitjacket hook failed; run ctx doctor."}
                if stage == "pre-tool-use" else
                {"output": "[ctx gate-failed] Tool output withheld because the hook process failed."})


def register(ctx):
    def before(tool_name, args, task_id="", **kwargs):
        decision = _bridge("pre-tool-use", tool_name, args, task_id, **kwargs)
        action = decision.get("action")
        if action == "rewrite":
            return {"action": "modify", "args": decision["input"]}
        if action in ("block", "ask"):
            return {"action": "approve" if action == "ask" else "block",
                    "message": decision.get("reason") or "Straitjacket requires approval."}

    def after(tool_name, args, result, task_id="", **kwargs):
        return _bridge("post-tool-use", tool_name, args, task_id, result=result, **kwargs).get("output")

    ctx.register_hook("pre_tool_call", before)
    ctx.register_hook("transform_tool_result", after)
