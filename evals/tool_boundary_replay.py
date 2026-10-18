#!/usr/bin/env python3
"""Model-free delivery ablation: auto vs digest on identical captured evidence.

No provider call, task-success estimate, latency comparison or billing claim.
Each row validates stream bytes and termination independently of delivery size.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from ctx.execution import run_capture
from ctx.store import Store
from ctx.toolresult import render_command_result
from ctx.workspace import resolve_workspace

CASES = (
    ("empty", "pass", 0),
    ("small", "print('hello')", 0),
    ("stderr-no-newline", "import sys; sys.stdout.write('left'); sys.stderr.write('right')", 0),
    ("failure", "import sys; print('failed-check'); sys.exit(4)", 4),
    ("signal", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)", None),
    ("escaped-json", "import sys; sys.stdout.write(chr(9) * 3000)", 0),
    ("flood", "for i in range(5000): print('quiet-needle' if i == 2136 else 'ordinary-log-' + str(i))", 0),
)


def evaluate(root: Path, state: Path) -> dict:
    from ctx.retrieval import get, Selector
    ws = resolve_workspace(str(root))
    store = Store(ws.workspace_id, state_root=state)
    rows = []
    try:
        for name, code, expected_exit in CASES:
            capture = run_capture(ws, [sys.executable, "-c", code], store=store,
                                  record_argv=["python", "-c", code])
            auto = render_command_result(ws, store, capture, delivery="auto")
            digest = render_command_result(ws, store, capture, delivery="digest")
            expected_status = capture.manifest["result"]
            status_ok = all(r.command_exit_code == expected_exit and r.signal == expected_status["signal"] for r in (auto, digest))
            raw = {s: store.get_blob(v["blob"][7:]) for s, v in capture.manifest["streams"].items()}
            inline_ok = auto.delivery != "inline" or (auto.stdout.encode() == raw["stdout"] and auto.stderr.encode() == raw["stderr"])
            retrieval_ok = True
            if name == "flood":
                retrieval_ok = "quiet-needle" in get(store, ws, auto.ref + "#stdout", Selector(lines=(2137, 2137)))
            rows.append({"case": name, "captured_bytes": sum(map(len, raw.values())),
                "auto_delivery": auto.delivery, "auto_wire_bytes": len(auto.to_json().encode()),
                "compact_wire_bytes": len(auto.to_model().encode()),
                "compact_status_and_ref": auto.ref in auto.to_model() and f"exit={expected_exit if expected_exit is not None else 'null'}" in auto.to_model(),
                "digest_wire_bytes": len(digest.to_json().encode()),
                "status_ok": status_ok, "inline_streams_exact": inline_ok, "retrieval_ok": retrieval_ok})
    finally:
        store.close()
    return {"schema": "ctx.tool-boundary-replay/v1", "kind": "model_free_delivery_ablation",
            "provider_calls": 0, "rows": rows,
            "passed": all(r["status_ok"] and r["compact_status_and_ref"] and r["inline_streams_exact"] and r["retrieval_ok"] for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sj-tool-replay-") as directory:
        base = Path(directory); root = base / "workspace"; root.mkdir()
        (root / "ctx.toml").write_text("version=1\n")
        report = evaluate(root, base / "state")
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True); args.out.write_text(text)
    else:
        print(text, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
