"""Acceptance: `ctx proxy` — pass-through-only observer for Anthropic-API traffic.

A fake upstream records the exact bytes it receives; every assertion about
the relay is byte-exact. Observation artifacts (window.json / wire.jsonl)
must carry usage ground truth and never a secret or a body.
"""

import http.client
import json
import os
import socket
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

JSON_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 40,
    "cache_creation_input_tokens": 300,
    "cache_read_input_tokens": 8500,
}
JSON_RESPONSE = json.dumps(
    {
        "id": "msg_01",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "ok"}],
        "usage": JSON_USAGE,
    }
).encode()

SSE_EVENTS = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_02","model":"claude-sonnet-5",'
    b'"usage":{"input_tokens":1200,"cache_creation_input_tokens":300,'
    b'"cache_read_input_tokens":8500,"output_tokens":2}}}\n\n',
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n\n',
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":40}}\n\n',
)

REQ_BODY = json.dumps(
    {
        "model": "claude-sonnet-5",
        "stream": False,
        "messages": [
            {"role": "user", "content": "hello there"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "X" * 500},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "Y" * 100},
                ],
            },
        ],
    }
).encode()


class _Upstream(BaseHTTPRequestHandler):
    """Fake Anthropic API: records exact request bytes+headers; replies with
    a JSON usage body, or SSE (split across writes) when "stream" is true."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"teapot"
        self.send_response(418)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.captured.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        stream = False
        try:
            stream = bool(json.loads(body).get("stream"))
        except Exception:
            pass
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in SSE_EVENTS:  # split each event across two flushes
                mid = len(event) // 2
                self.wfile.write(event[:mid])
                self.wfile.flush()
                time.sleep(0.02)
                self.wfile.write(event[mid:])
                self.wfile.flush()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(JSON_RESPONSE)))
            self.end_headers()
            self.wfile.write(JSON_RESPONSE)


@pytest.fixture()
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    srv.captured = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def proxy(upstream, tmp_path):
    from ctx.proxy import _make_server

    state = tmp_path / "proxy-state"
    srv = _make_server(
        0, f"http://127.0.0.1:{upstream.server_address[1]}", state, "ws-test"
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, state
    srv.shutdown()
    srv.server_close()


def _post(port: int, path: str, body: bytes, extra_headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Authorization": "Bearer sk-ant-SECRET-TOKEN",
        "x-api-key": "sk-ant-api-KEY",
    }
    headers.update(extra_headers or {})
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    conn.close()
    return status, data


def _wait_window(state: Path, requests: int, timeout: float = 5.0) -> dict:
    """Observation lands just after the last relayed byte; poll briefly."""
    path = state / "window.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("requests") == requests:
                return doc
        time.sleep(0.02)
    raise AssertionError(f"window.json did not reach requests={requests}")


def _wait_wire(state: Path, count: int, timeout: float = 5.0) -> list[dict]:
    path = state / "wire.jsonl"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) >= count:
                return [json.loads(line) for line in lines]
        time.sleep(0.02)
    raise AssertionError(f"wire.jsonl did not reach {count} records")


def test_json_passthrough_byte_exact_and_window(proxy, upstream):
    srv, state = proxy
    port = srv.server_address[1]

    status, data = _post(port, "/v1/messages", REQ_BODY)
    assert status == 200
    assert data == JSON_RESPONSE  # byte-exact response pass-through

    captured = upstream.captured[0]
    assert captured["body"] == REQ_BODY  # byte-exact request pass-through
    assert captured["path"] == "/v1/messages"
    # Auth headers are forwarded to upstream (relay does not strip them) ...
    assert captured["headers"]["authorization"] == "Bearer sk-ant-SECRET-TOKEN"
    assert captured["headers"]["x-api-key"] == "sk-ant-api-KEY"
    # ... and Host is rewritten to the upstream authority.
    assert captured["headers"]["host"] == f"127.0.0.1:{upstream.server_address[1]}"

    window = _wait_window(state, requests=1)
    assert window["model"] == "claude-sonnet-5"
    assert window["last_input_tokens"] == 1200 + 8500 + 300
    assert window["context_limit"] == 200000
    assert window["window_pct"] == 5.0  # 10000 / 200000
    assert window["cum_cache_read"] == 8500
    assert window["cum_cache_creation"] == 300
    assert window["cum_output"] == 40
    assert window["workspace_id"] == "ws-test"

    lines = (state / "wire.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["seq"] == 1
    assert rec["path"] == "/v1/messages"
    assert rec["status"] == 200
    assert rec["req_bytes"] == len(REQ_BODY)
    assert rec["messages"] == 3
    assert rec["blocks"] == {"text": 2, "tool_use": 1, "tool_result": 2}
    assert rec["tool_result_top"][0] > rec["tool_result_top"][1]  # sorted desc
    assert rec["usage"] == JSON_USAGE


def test_no_secrets_or_bodies_persisted(proxy):
    srv, state = proxy
    _post(srv.server_address[1], "/v1/messages", REQ_BODY)
    _wait_window(state, requests=1)
    for name in ("wire.jsonl", "window.json"):
        text = (state / name).read_text(encoding="utf-8")
        assert "authorization" not in text.lower()
        assert "x-api-key" not in text.lower()
        assert "sk-ant" not in text
        assert "hello there" not in text  # no request body content
        assert "XXXX" not in text  # no tool_result content


def test_sse_stream_relayed_completely(proxy, upstream):
    srv, state = proxy
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "stream": True,
            "messages": [{"role": "user", "content": "go"}],
        }
    ).encode()
    status, data = _post(
        srv.server_address[1],
        "/v1/messages",
        body,
        {"anthropic-beta": "context-1m-2025-08-07"},
    )
    assert status == 200
    assert data == b"".join(SSE_EVENTS)  # every event byte reached the client
    assert upstream.captured[0]["body"] == body

    window = _wait_window(state, requests=1)
    assert window["last_input_tokens"] == 1200 + 8500 + 300
    assert window["context_limit"] == 1000000  # 1m beta header seen
    assert window["window_pct"] == 1.0
    assert window["cum_output"] == 40  # message_delta wins over message_start

    rec = json.loads((state / "wire.jsonl").read_text(encoding="utf-8"))
    assert rec["usage"] == JSON_USAGE


def test_corrupt_request_body_still_relays(proxy, upstream):
    srv, state = proxy
    corrupt = b'{"broken json'
    status, data = _post(srv.server_address[1], "/v1/messages", corrupt)
    assert status == 200
    assert data == JSON_RESPONSE  # observation failure never breaks the relay
    assert upstream.captured[0]["body"] == corrupt
    rec = _wait_wire(state, 1)[0]
    assert rec["messages"] == 0
    assert rec["req_bytes"] == len(corrupt)
    assert rec["usage"] == JSON_USAGE  # response usage still observed


def test_non_messages_path_passthrough_and_status(proxy):
    srv, state = proxy
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert resp.status == 418  # arbitrary status preserved
    assert resp.read() == b"teapot"
    conn.close()
    rec = _wait_wire(state, 1)[0]
    assert rec == {
        "seq": 1,
        "path": "/health",
        "status": 418,
        "req_bytes": 0,
        "messages": 0,
        "blocks": {},
        "tool_result_top": [],
        "usage": {},
    }


def test_chunked_request_body_forwarded(proxy, upstream):
    srv, _ = proxy
    payload = json.dumps({"model": "claude-sonnet-5", "messages": []}).encode()
    sock = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=10)
    sock.sendall(
        b"POST /v1/messages HTTP/1.1\r\nHost: local\r\n"
        b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
    )
    for i in range(0, len(payload), 10):
        chunk = payload[i : i + 10]
        sock.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
    sock.sendall(b"0\r\n\r\n")
    raw = b""
    while True:
        piece = sock.recv(65536)
        if not piece:
            break
        raw += piece
    sock.close()
    assert raw.split(b"\r\n", 1)[0].endswith(b"200 OK")
    assert raw.endswith(JSON_RESPONSE)
    assert upstream.captured[0]["body"] == payload  # de-chunked, byte-identical


def test_context_limit_table():
    from ctx.proxy import _context_limit

    assert _context_limit("claude-sonnet-5", False) == 200000
    assert _context_limit("claude-sonnet-5[1m]", False) == 1000000
    assert _context_limit("claude-sonnet-5", True) == 1000000
    assert _context_limit("claude-opus-4-6", True) == 200000
    assert _context_limit("claude-haiku-4-5", False) == 200000
    assert _context_limit("", False) == 200000


def test_binds_loopback_only(proxy):
    srv, _ = proxy
    assert srv.server_address[0] == "127.0.0.1"


# ------------------------------------------------------- wrap integration
def test_wrap_claude_proxy_integration(tmp_path, monkeypatch, upstream):
    from ctx.wrap import wrap_claude

    ws = tmp_path / "proj"
    ws.mkdir()
    out_file = tmp_path / "resp.bin"
    base_url_file = tmp_path / "base_url.txt"

    fetch_py = tmp_path / "fetch.py"
    fetch_py.write_text(
        "import json, os, time, urllib.request\n"
        "body = json.dumps({'model': 'claude-sonnet-5', 'stream': False,\n"
        "                   'messages': [{'role': 'user', 'content': 'hi'}]}).encode()\n"
        "req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages',\n"
        "    data=body, headers={'Content-Type': 'application/json',\n"
        "                        'Authorization': 'Bearer sk-wrap-secret'})\n"
        f"open({str(out_file)!r}, 'wb').write(urllib.request.urlopen(req, timeout=10).read())\n"
        f"open({str(base_url_file)!r}, 'w').write(os.environ['ANTHROPIC_BASE_URL'])\n"
        "time.sleep(0.3)\n",  # let the proxy's observation record land
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--help" ]; then echo "usage: claude [--settings <file>]"; exit 0; fi\n'
        f"exec {sys.executable} {fetch_py}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", upstream_url)
    monkeypatch.setenv("PYTHONPATH", str(SRC))  # so `python -m ctx proxy` imports

    rc = wrap_claude(ws, [], ctx_exe=f"{sys.executable} -m ctx", use_proxy=True)
    assert rc == 0

    # The fake claude reached the fake upstream *through* the proxy.
    assert out_file.read_bytes() == JSON_RESPONSE
    base_url = base_url_file.read_text(encoding="utf-8").strip()
    assert base_url.startswith("http://127.0.0.1:")
    assert base_url != upstream_url  # child env pointed at the proxy ...
    assert os.environ["ANTHROPIC_BASE_URL"] == upstream_url  # ... parent untouched
    assert upstream.captured[0]["headers"]["authorization"] == "Bearer sk-wrap-secret"

    window = ws / ".ctx-session-reads" / "proxy" / "window.json"
    assert window.is_file()
    doc = json.loads(window.read_text(encoding="utf-8"))
    assert doc["requests"] == 1
    assert doc["last_input_tokens"] == 1200 + 8500 + 300

    # The proxy subprocess is gone: its port must refuse connections.
    port = int(base_url.rsplit(":", 1)[1])
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                time.sleep(0.05)
        except OSError:
            break
    else:
        raise AssertionError("proxy still accepting connections after wrap exit")
