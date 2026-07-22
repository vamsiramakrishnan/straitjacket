#!/usr/bin/env python3
"""ctx-account — systematic context accounting for `ctx wrap`.

Answers "what does each piece of the harness cost in the model's context
window?" the honest way: by capturing the **actual agent request body at the
wire** for a controlled matrix of cells, in one identical environment, and
reporting per-component deltas.

Why a matrix and not a single measurement: token *usage* numbers
(cache_create/cache_read) are confounded — by prompt-cache warmth, by turn
count, and (fatally, in a nested-agent harness) by an ambient tool suite that
the host injects into every child call and that changes as MCP servers come
and go. Those confounds vanish under two disciplines:

  1. **Capture the request body, not the usage.** A local stand-in server
     (ctx_capture.decompose) logs exactly what Claude Code put on the wire —
     system / tools / messages — then returns a stub. Zero upstream cost.

  2. **Attribute by delta within one run.** Every cell carries the same
     ambient tool schemas, so they cancel: (cell − bare) is that component's
     true marginal cost, immune to whatever the host injected.

Cells (each: fresh CLAUDE_CONFIG_DIR, fresh empty workspace, same model, same
--allowedTools, 1 trivial turn, same capture server):

  bare            plain `claude`
  +claudemd       bare + a CLAUDE.md verb card in the workspace
  +append         bare + --append-system-prompt (ctx output-discipline)
  +agent          bare + .claude/agents/ctx-explorer.md registered
  +settings       bare + --settings <ctx hook settings>
  ctx-wrap        the real `ctx wrap claude --proxy` (all of the above)

Output: an attribution table (component -> Δsystem/Δtools/Δmessages/Δtotal)
as JSON, and — with --html — feeds ctx_anatomy to render the visualizer.

Usage:
  ctx_account.py --out DIR [--model haiku] [--html DIR/context.html]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ctx_capture  # noqa: E402  (decompose lives here)

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, SRC)

MODELS = {"haiku": "claude-haiku-4-5-20251001"}
TOOLS = "Bash Read Grep Glob Edit Write MultiEdit"
PROMPT = "Reply with exactly: OK"


# ── in-process capture server ─────────────────────────────────────────────
class _Capture:
    """Threaded HTTP server standing in for api.anthropic.com. Records every
    /v1/messages body (decomposed) and returns a stub — no upstream call."""

    def __init__(self):
        self.bodies: list[dict] = []
        self._srv = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def _handler(self):
        outer = self

    # noqa
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _read(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                if self.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                return raw

            def do_POST(self):
                raw = self._read()
                if "/v1/messages" in self.path:
                    try:
                        outer.bodies.append(ctx_capture.decompose(json.loads(raw)))
                    except Exception as e:
                        outer.bodies.append({"error": str(e)})
                resp = json.dumps({
                    "id": "msg_stub", "type": "message", "role": "assistant",
                    "model": "claude-haiku-4-5-20251001",
                    "content": [{"type": "text", "text": "OK"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        return H

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._srv.shutdown()

    def agent_request(self) -> dict | None:
        reqs = [b for b in self.bodies if "est_tokens" in b]
        return max(reqs, key=lambda r: r.get("tools_count", 0)) if reqs else None

    def reset(self):
        self.bodies.clear()


# ── cell setup ────────────────────────────────────────────────────────────
def _verb_card() -> str:
    return ("# Repository harness (ctx / straitjacket)\n\n"
            "Prefer bounded verbs over search/read loops:\n"
            "- `ctx ask \"<q>\" --intent locate|impact|diagnose|trace`\n"
            "- `ctx q '<pipeline>'` — typed streams\n"
            "- `ctx run -- <cmd>` / `ctx get <handle>`\n")


def _cells(out: pathlib.Path):
    from ctx import wrap
    from ctx.installer import _template_dir
    od = wrap._OUTPUT_DISCIPLINE
    agent_src = _template_dir() / "agents" / wrap._AGENT_FILENAME
    settings = json.dumps(wrap.prepare_claude(out, "ctx"))

    def ws_bare(ws): pass

    def ws_card(ws): (ws / "CLAUDE.md").write_text(_verb_card())

    def ws_agent(ws):
        d = ws / ".claude" / "agents"
        d.mkdir(parents=True)
        (d / "ctx-explorer.md").write_bytes(agent_src.read_bytes())

    def base_argv(model):
        a = ["claude", "-p", PROMPT, "--max-turns", "1", "--output-format", "json",
             "--allowedTools", TOOLS]
        if MODELS.get(model):
            a += ["--model", MODELS[model]]
        return a

    def append_argv(model):
        a = base_argv(model)
        return a[:2] + ["--append-system-prompt", od] + a[2:]

    def settings_argv(model, spath):
        a = base_argv(model)
        return a[:1] + ["--settings", str(spath)] + a[1:]

    def wrap_argv(model):
        return ["ctx", "wrap", "claude", "--proxy", "--"] + base_argv(model)[1:]

    # (label, workspace-setup, argv-builder, needs-settings-file)
    return [
        ("bare", ws_bare, base_argv, False),
        ("+claudemd", ws_card, base_argv, False),
        ("+append", ws_bare, append_argv, False),
        ("+agent", ws_agent, base_argv, False),
        ("+settings", ws_bare, settings_argv, True),
        ("ctx-wrap", ws_bare, wrap_argv, False),
    ], settings


def run_cell(cap: _Capture, out: pathlib.Path, label, setup, argv_fn,
             needs_settings, settings_json, model) -> dict:
    ws = out / f"ws_{label.strip('+')}"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    setup(ws)
    cfg = out / f"cfg_{label.strip('+')}"
    if cfg.exists():
        shutil.rmtree(cfg)
    cfg.mkdir(parents=True)
    if needs_settings:
        spath = out / f"settings_{label.strip('+')}.json"
        spath.write_text(settings_json)
        argv = argv_fn(model, spath)
    else:
        argv = argv_fn(model)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg),
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{cap.port}"}
    cap.reset()
    try:
        subprocess.run(argv, cwd=ws, env=env, capture_output=True,
                       text=True, timeout=90)
    except subprocess.TimeoutExpired:
        pass
    # the proxy (ctx-wrap cell) relays asynchronously; give it a beat
    for _ in range(20):
        if cap.agent_request():
            break
        time.sleep(0.25)
    a = cap.agent_request()
    if not a:
        return {"label": label, "captured": False}
    et = a["est_tokens"]
    return {"label": label, "captured": True,
            "system": et["system"], "tools": et["tools"],
            "messages": et["messages"], "total": et["total"],
            "tools_count": a["tools_count"], "tool_leaderboard":
            [{"name": n, "tok": b // 4} for n, b in a.get("tool_sizes", [])]}


def attribute(rows: list) -> dict:
    by = {r["label"]: r for r in rows if r.get("captured")}
    bare = by.get("bare")
    table = []
    if bare:
        for r in rows:
            if not r.get("captured") or r["label"] == "bare":
                continue
            table.append({
                "component": r["label"],
                "d_system": r["system"] - bare["system"],
                "d_tools": r["tools"] - bare["tools"],
                "d_messages": r["messages"] - bare["messages"],
                "d_total": r["total"] - bare["total"],
            })
    return {"bare": bare, "cells": rows, "attribution": table}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="haiku", choices=list(MODELS))
    ap.add_argument("--html", type=pathlib.Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    cells, settings_json = _cells(args.out)
    rows = []
    with _Capture() as cap:
        for label, setup, argv_fn, needs in cells:
            print(f"[{label}] capturing…", flush=True)
            row = run_cell(cap, args.out, label, setup, argv_fn, needs,
                           settings_json, args.model)
            rows.append(row)
            if row.get("captured"):
                print(f"  system={row['system']} tools={row['tools']} "
                      f"messages={row['messages']} total={row['total']}", flush=True)
            else:
                print("  NOT CAPTURED", flush=True)

    result = attribute(rows)
    (args.out / "accounting.json").write_text(json.dumps(result, indent=2))
    print("\n=== attribution (Δ vs bare) ===")
    print(f"{'component':14s} {'Δsystem':>8s} {'Δtools':>7s} {'Δmsgs':>7s} {'Δtotal':>7s}")
    for t in result["attribution"]:
        print(f"{t['component']:14s} {t['d_system']:>+8d} {t['d_tools']:>+7d} "
              f"{t['d_messages']:>+7d} {t['d_total']:>+7d}")
    print(f"\nwrote {args.out / 'accounting.json'}")

    if args.html:
        _render(result, args.html)
        print(f"wrote {args.html}")
    return 0


def _render(result: dict, html_path: pathlib.Path):
    """Hand the controlled cells to ctx_anatomy's renderer as composition +
    a controlled-attribution block."""
    import ctx_anatomy
    by = {r["label"]: r for r in result["cells"] if r.get("captured")}
    compose = []
    for lab in ("bare", "ctx-wrap"):
        if lab in by:
            r = by[lab]
            shown = sum(t["tok"] for t in r["tool_leaderboard"])
            compose.append({"label": lab, "system": r["system"], "tools": r["tools"],
                            "messages": r["messages"], "total": r["total"],
                            "tools_count": r["tools_count"],
                            "tool_leaderboard": r["tool_leaderboard"],
                            "tools_other": max(0, r["tools"] - shown)})
    anatomy = {"composition": compose, "controlled_attribution": result["attribution"]}
    if len(compose) == 2:
        a, b = compose
        anatomy["ctx_delta"] = {
            "system": b["system"] - a["system"], "tools": b["tools"] - a["tools"],
            "messages": b["messages"] - a["messages"], "total": b["total"] - a["total"],
            "pct": round((b["total"] - a["total"]) / a["total"] * 100, 1) if a["total"] else None}
    html_path.write_text(ctx_anatomy.render_html(anatomy), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
