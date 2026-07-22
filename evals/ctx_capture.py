#!/usr/bin/env python3
"""Zero-cost request-body capture for context accounting.

Stands in for api.anthropic.com: decomposes the first /v1/messages body into
system / tools / messages sizes, then returns a minimal valid response so
`claude` exits cleanly. No upstream call is ever made, so capturing a request
costs nothing.

``decompose(body)`` is importable (used by ctx_account.py's in-process
server). Run as a script for a standalone logging server:

  ctx_capture.py <out.jsonl> <label> <port>
"""
from __future__ import annotations

import gzip
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def _est_tok(nbytes: int) -> int:  # rough 4 bytes/token for schema/prose
    return nbytes // 4


def decompose(body: dict) -> dict:
    """Break a /v1/messages request body into system / tools / messages sizes,
    plus a per-tool schema leaderboard. Sizes are JSON bytes; est_tokens ≈ /4."""
    def sz(x):
        return len(json.dumps(x, ensure_ascii=False).encode()) if x is not None else 0
    system = body.get("system")
    tools = body.get("tools") or []
    messages = body.get("messages") or []
    comp = {
        "system_bytes": sz(system),
        "tools_bytes": sz(tools),
        "tools_count": len(tools),
        "messages_bytes": sz(messages),
        "messages_count": len(messages),
        "total_bytes": sz(body),
    }
    comp["tool_sizes"] = sorted(
        [(t.get("name", "?"), sz(t)) for t in tools], key=lambda kv: -kv[1])[:24]
    if isinstance(system, list):
        comp["system_blocks"] = [
            {"len": len(b.get("text", "")), "head": b.get("text", "")[:80]}
            for b in system if isinstance(b, dict)]
    elif isinstance(system, str):
        comp["system_blocks"] = [{"len": len(system), "head": system[:80]}]
    comp["est_tokens"] = {k.replace("_bytes", ""): _est_tok(v)
                          for k, v in comp.items() if k.endswith("_bytes")}
    return comp


def _serve(out: pathlib.Path, label: str, port: int) -> None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _read(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            if self.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return raw

        def do_POST(self):
            raw = self._read()
            if "/v1/messages" in self.path:
                try:
                    comp = decompose(json.loads(raw))
                    comp["label"] = label
                    with out.open("a") as fh:
                        fh.write(json.dumps(comp) + "\n")
                except Exception as e:
                    with out.open("a") as fh:
                        fh.write(json.dumps({"error": str(e)}) + "\n")
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

    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    _serve(pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]))
