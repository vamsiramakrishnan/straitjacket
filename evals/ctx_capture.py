#!/usr/bin/env python3
"""Zero-cost request-body capture. Stands in for api.anthropic.com: logs the
first /v1/messages body decomposed into system / tools / messages sizes, then
returns a minimal valid response so `claude` exits cleanly. No upstream call."""
import gzip, json, sys, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = pathlib.Path(sys.argv[1])
LABEL = sys.argv[2]
PORT = int(sys.argv[3])



def _est_tok(nbytes):  # rough 4 bytes/token
    return nbytes // 4


def decompose(body: dict) -> dict:
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
    # per-tool sizes (which schema is heavy)
    comp["tool_sizes"] = sorted(
        [(t.get("name", "?"), sz(t)) for t in tools],
        key=lambda kv: -kv[1])[:20]
    # split system into blocks if it's a list (Claude Code sends system as blocks)
    if isinstance(system, list):
        comp["system_blocks"] = [
            {"len": len(b.get("text", "")), "head": (b.get("text", "")[:80])}
            for b in system if isinstance(b, dict)]
    elif isinstance(system, str):
        comp["system_blocks"] = [{"len": len(system), "head": system[:80]}]
    comp["est_tokens"] = {k.replace("_bytes", ""): _est_tok(v)
                          for k, v in comp.items() if k.endswith("_bytes")}
    return comp


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
                body = json.loads(raw)
                comp = decompose(body); comp["label"]=LABEL
                with OUT.open("a") as fh: fh.write(json.dumps(comp)+"\n")
            except Exception as e:
                with OUT.open("a") as fh: fh.write(json.dumps({"error":str(e)})+"\n")
        # canned minimal response so claude terminates
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


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
