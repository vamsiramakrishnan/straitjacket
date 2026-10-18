#!/usr/bin/env python3
"""Model-free workflow checks; scripted tool calls are not model turns."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile

from ctx.sdk import ToolSession
from tool_boundary_replay import evaluate


async def navigation(root):
    path = root / "module.py"
    path.write_text("".join(f"row_{n} = '" + "x"*60 + "'\n" for n in range(300)) +
                    "def target():\n    return 'decisive-marker'\n")
    session = ToolSession(root, context_epoch="replay-context-1")
    first = await session.get("repo:module.py", start_line=1, end_line=25)
    repeated = await session.get("repo:module.py", start_line=1, end_line=25)
    path.write_text(path.read_text().replace("row_10 = '" + "x"*60 + "'", "row_10 = 'changed-marker'"))
    changed = await session.get("repo:module.py", start_line=1, end_line=25)
    session.set_context_epoch("replay-context-2")
    reset = await session.get("repo:module.py", start_line=1, end_line=25)
    outline = await session.navigate("module.py")
    symbol = await session.navigate("module.py", symbol="target")
    composed = await session.query("search decisive-marker --glob '*.py' | top 1 | get --context 0")
    return {"first_bytes": len(first.encode()), "repeated_bytes": len(repeated.encode()),
        "changed_bytes": len(changed.encode()), "reset_bytes": len(reset.encode()),
        "repeat_body_suppressed": "\nL1:" not in repeated and "reused: 25" in repeated,
        "changed_line_delivered": "changed-marker" in changed,
        "epoch_restored_lines": "\nL1:" in reset,
        "outline_then_symbol": "target" in outline and "decisive-marker" in symbol,
        "composed_needle_delivered": "decisive-marker" in composed,
        "scripted_custom_tool_calls": session.metrics["custom_tool_calls"],
        "model_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ctx-workflow-replay-") as directory:
        base = Path(directory); root = base / "workspace"; root.mkdir()
        (root / "ctx.toml").write_text("version=1\n")
        old = os.environ.get("CTX_STATE_HOME")
        os.environ["CTX_STATE_HOME"] = str(base / "state")
        try:
            command = evaluate(root, base / "command-state")
            views = asyncio.run(navigation(root))
        finally:
            if old is None:
                os.environ.pop("CTX_STATE_HOME", None)
            else:
                os.environ["CTX_STATE_HOME"] = old
    report = {"schema": "ctx.workflow-replay/v1", "kind": "model_free",
        "provider_calls": 0, "commands": command, "navigation": views,
        "passed": command["passed"] and all(views[k] for k in (
            "repeat_body_suppressed", "changed_line_delivered", "epoch_restored_lines",
            "outline_then_symbol", "composed_needle_delivered")),
        "claim": "delivery_and_evidence_only_not_task_cost_or_turn_reductions"}
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        print(text, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
