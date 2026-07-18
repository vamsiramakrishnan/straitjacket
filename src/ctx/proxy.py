"""Tier-0 observer proxy: pass-through-only relay for Anthropic-API traffic.

``ctx proxy`` binds 127.0.0.1 and forwards every request verbatim to one
upstream, streaming the response back unbuffered. Along the way it observes
ground truth the hook tier cannot see — true token usage and window
fullness — and writes it to ``<state_dir>/window.json`` (atomic snapshot)
and ``<state_dir>/wire.jsonl`` (one line per exchange).

Invariants: not a single byte of any request or response body is mutated
(compressed upstream responses are relayed compressed; decompression happens
only on the observer's private copy); observation is fail-open (an
observation error never breaks the relay); Authorization/x-api-key headers
and bodies are never logged or persisted.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_CHUNK = 8192  # relay granularity: small enough to preserve SSE latency
_OBSERVE_CAP = 8 * 1024 * 1024  # stop accumulating response bytes for observation
_POOL_MAX = 4  # idle upstream connections kept warm (TLS handshake amortization)

# Hop-by-hop headers are owned by each connection leg, never forwarded.
# Accept-Encoding passes through untouched: the client negotiates compression
# with the upstream as if the relay were not there, and observation
# decompresses its own copy of the stream (see _Decoder).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
# Loose scan for usage counters in relayed SSE text — observing, not parsing.
_USAGE_KEY_RE = re.compile(
    rb'"(input_tokens|output_tokens|cache_creation_input_tokens|cache_read_input_tokens)"'
    rb"\s*:\s*(\d+)"
)

_DEFAULT_CONTEXT_LIMIT = 200_000


def _context_limit(model: str, beta_1m_header: bool) -> int:
    if model.startswith("claude-sonnet-5") and ("[1m]" in model or beta_1m_header):
        return 1_000_000
    return _DEFAULT_CONTEXT_LIMIT  # opus, haiku, unknown


# ------------------------------------------------------------- observation
def _observe_request(path: str, body: bytes) -> dict:
    """Shape facts about a /messages request body. Fail-open: any parse
    problem degrades to byte counts only. Never returns header or body text."""
    obs: dict = {
        "req_bytes": len(body),
        "messages": 0,
        "blocks": {},
        "tool_result_top": [],
        "model": "",
        "stream": False,
    }
    if not path.split("?", 1)[0].endswith("/messages"):
        return obs
    try:
        doc = json.loads(body)
        obs["model"] = str(doc.get("model") or "")
        obs["stream"] = bool(doc.get("stream"))
        msgs = doc.get("messages") or []
        obs["messages"] = len(msgs)
        blocks: dict[str, int] = {}
        tool_result_sizes: list[int] = []
        for msg in msgs:
            content = msg.get("content")
            if isinstance(content, str):
                blocks["text"] = blocks.get("text", 0) + 1
                continue
            for block in content or []:
                btype = str(block.get("type") or "unknown")
                blocks[btype] = blocks.get(btype, 0) + 1
                if btype == "tool_result":
                    tool_result_sizes.append(
                        len(json.dumps(block.get("content", ""), ensure_ascii=False))
                    )
        obs["blocks"] = blocks
        obs["tool_result_top"] = sorted(tool_result_sizes, reverse=True)[:3]
    except Exception:
        pass
    return obs


class _UsageScanner:
    """Incremental usage-counter scan over relayed SSE chunks. A small tail
    is carried so fragments split across chunk boundaries still match;
    later occurrences win (message_delta carries the final output_tokens)."""

    def __init__(self) -> None:
        self._tail = b""
        self.usage: dict[str, int] = {}

    def feed(self, chunk: bytes) -> None:
        try:
            buf = self._tail + chunk
            for m in _USAGE_KEY_RE.finditer(buf):
                self.usage[m.group(1).decode("ascii")] = int(m.group(2))
            self._tail = buf[-2048:]
        except Exception:
            self._tail = b""


class _Decoder:
    """Incremental content-decoding for the observer's copy of the response.

    The relayed bytes are never touched. gzip/deflate decode via zlib with
    header auto-detection; identity passes through; any other encoding (br,
    zstd) or a decode error disables observation for the exchange — the
    relay itself is indifferent to what it carries."""

    def __init__(self, encoding: str) -> None:
        enc = (encoding or "").strip().lower()
        self._z = None
        self.ok = enc in ("", "identity", "gzip", "x-gzip", "deflate")
        if self.ok and enc not in ("", "identity"):
            self._z = zlib.decompressobj(32 + zlib.MAX_WBITS)  # gzip/zlib auto

    def feed(self, chunk: bytes) -> bytes:
        if not self.ok:
            return b""
        if self._z is None:
            return chunk
        try:
            return self._z.decompress(chunk)
        except zlib.error:
            self.ok = False
            return b""


def _usage_from_json(body: bytes) -> dict[str, int]:
    try:
        u = json.loads(body).get("usage") or {}
        return {k: u[k] for k in _USAGE_KEYS if isinstance(u.get(k), int)}
    except Exception:
        return {}


class _Observer:
    """Cumulative wire observations persisted to window.json + wire.jsonl.
    Every entry point is fail-open; nothing here can break the relay."""

    def __init__(self, state_dir: Path, workspace_id: str) -> None:
        self._dir = Path(state_dir)
        self._workspace_id = workspace_id
        self._lock = threading.Lock()
        self._requests = 0
        self._cum = {"cache_read": 0, "cache_creation": 0, "output": 0}

    def record(
        self,
        *,
        path: str,
        status: int,
        req_obs: dict,
        usage: dict[str, int],
        beta_1m_header: bool,
        ms: dict[str, float] | None = None,
        reused_conn: bool = False,
    ) -> None:
        try:
            with self._lock:
                self._requests += 1
                seq = self._requests
                self._cum["cache_read"] += usage.get("cache_read_input_tokens", 0)
                self._cum["cache_creation"] += usage.get("cache_creation_input_tokens", 0)
                self._cum["output"] += usage.get("output_tokens", 0)
                self._append_wire(seq, path, status, req_obs, usage, ms, reused_conn)
                self._write_window(req_obs, usage, beta_1m_header)
        except Exception:
            pass

    def _append_wire(
        self,
        seq: int,
        path: str,
        status: int,
        req_obs: dict,
        usage: dict[str, int],
        ms: dict[str, float] | None,
        reused_conn: bool,
    ) -> None:
        record = {
            "seq": seq,
            "path": path.split("?", 1)[0],
            "status": status,
            "req_bytes": req_obs.get("req_bytes", 0),
            "messages": req_obs.get("messages", 0),
            "blocks": req_obs.get("blocks", {}),
            "tool_result_top": req_obs.get("tool_result_top", []),
            "usage": dict(sorted(usage.items())),
            "ms": {k: round(v, 1) for k, v in sorted((ms or {}).items())},
            "reused_conn": reused_conn,
        }
        with (self._dir / "wire.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def _write_window(
        self, req_obs: dict, usage: dict[str, int], beta_1m_header: bool
    ) -> None:
        model = str(req_obs.get("model") or "")
        last_input = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        limit = _context_limit(model, beta_1m_header)
        doc = {
            "model": model,
            "last_input_tokens": last_input,
            "context_limit": limit,
            "window_pct": round(100 * last_input / limit, 1),
            "requests": self._requests,
            "cum_cache_read": self._cum["cache_read"],
            "cum_cache_creation": self._cum["cache_creation"],
            "cum_output": self._cum["output"],
            "workspace_id": self._workspace_id,
        }
        tmp = self._dir / "window.json.tmp"
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._dir / "window.json")


# -------------------------------------------------------------------- relay
class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Set by _make_server:
    ctx_upstream = None  # urlsplit result
    ctx_observer: _Observer | None = None
    ctx_ssl: ssl.SSLContext | None = None
    ctx_pool: list | None = None  # idle upstream connections
    ctx_pool_lock: threading.Lock | None = None


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass  # never log requests: paths may sit next to secrets in URLs

    # One relay routine serves every method.
    def _relay(self) -> None:
        t_start = time.monotonic()
        try:
            body = self._read_request_body()
        except Exception:
            self.close_connection = True
            return

        server: _ProxyServer = self.server  # type: ignore[assignment]
        resp = conn = None
        reused = False
        connect_ms = ttfb_ms = 0.0
        # A pooled connection can be stale (upstream idled it out); retry
        # exactly once on a fresh one. A fresh-connection failure is real.
        for _attempt in (0, 1):
            conn, reused = self._acquire(server)
            try:
                t0 = time.monotonic()
                if not reused:
                    conn.connect()
                    connect_ms = (time.monotonic() - t0) * 1000
                t_send = time.monotonic()
                self._send_upstream(server, conn, body)
                resp = conn.getresponse()
                ttfb_ms = (time.monotonic() - t_send) * 1000
                break
            except Exception:
                conn.close()
                resp = None
                if not reused:
                    self._bad_gateway()
                    return
        if resp is None:  # two stale pooled connections in a row
            self._bad_gateway()
            return

        status = resp.status
        try:
            self.send_response_only(status, resp.reason)
            for name, value in resp.getheaders():
                if name.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(name, value)
            # The relay is close-delimited toward the client: simplest framing
            # that stays valid whether upstream sent a length or chunks.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
        except Exception:
            conn.close()
            self.close_connection = True
            return

        is_sse = "text/event-stream" in (resp.getheader("Content-Type") or "")
        decoder = _Decoder(resp.getheader("Content-Encoding") or "")
        scanner = _UsageScanner()
        accumulated = bytearray()
        upstream_done = False
        try:
            # Unbuffered relay: read whatever bytes are available (read1 does
            # not wait to fill the window) and write+flush immediately, so
            # each SSE event reaches the client as soon as upstream emits it.
            while True:
                chunk = resp.read1(_CHUNK)
                if not chunk:
                    upstream_done = True
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                try:  # observation only — never allowed to break the relay
                    if is_sse:
                        scanner.feed(decoder.feed(chunk))
                    elif len(accumulated) < _OBSERVE_CAP:
                        accumulated += decoder.feed(chunk)
                except Exception:
                    pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client went away mid-stream; nothing to salvage
        finally:
            if upstream_done and not resp.will_close:
                self._release(server, conn)
            else:
                conn.close()
        total_ms = (time.monotonic() - t_start) * 1000

        try:
            usage = scanner.usage if is_sse else _usage_from_json(bytes(accumulated))
            beta_1m = "1m" in (self.headers.get("anthropic-beta") or "")
            server.ctx_observer.record(
                path=self.path,
                status=status,
                req_obs=_observe_request(self.path, body),
                usage=usage,
                beta_1m_header=beta_1m,
                ms={"connect": connect_ms, "ttfb": ttfb_ms, "total": total_ms},
                reused_conn=reused,
            )
        except Exception:
            pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _relay

    def _send_upstream(
        self, server: _ProxyServer, conn: http.client.HTTPConnection, body: bytes
    ) -> None:
        upstream = server.ctx_upstream
        fwd_path = self.path
        if upstream.path and upstream.path != "/":
            fwd_path = upstream.path.rstrip("/") + self.path
        conn.putrequest(self.command, fwd_path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", upstream.netloc)
        has_length = False
        for name, value in self.headers.items():
            if name.lower() in _HOP_BY_HOP:
                continue
            if name.lower() == "content-length":
                has_length = True
            conn.putheader(name, value)
        if body and not has_length:  # request arrived chunked
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body if body else None)

    def _acquire(self, server: _ProxyServer) -> tuple[http.client.HTTPConnection, bool]:
        with server.ctx_pool_lock:
            if server.ctx_pool:
                return server.ctx_pool.pop(), True
        u = server.ctx_upstream
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(
                u.hostname, u.port or 443, context=server.ctx_ssl, timeout=600
            )
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
        return conn, False

    def _release(self, server: _ProxyServer, conn: http.client.HTTPConnection) -> None:
        with server.ctx_pool_lock:
            if len(server.ctx_pool) < _POOL_MAX:
                server.ctx_pool.append(conn)
                return
        conn.close()

    def _read_request_body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = bytearray()
            while True:
                size_line = self.rfile.readline(65536).strip()
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    while True:  # drain trailers up to the blank line
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return bytes(out)
                out += self._read_exact(size)
                self.rfile.readline(65536)  # chunk-terminating CRLF
        length = int(self.headers.get("Content-Length") or 0)
        return self._read_exact(length) if length > 0 else b""

    def _read_exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            piece = self.rfile.read(n - len(out))
            if not piece:
                break
            out += piece
        return bytes(out)

    def _bad_gateway(self) -> None:
        try:
            payload = b'{"error":"ctx proxy: upstream unreachable"}'
            self.send_response_only(502, "Bad Gateway")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            pass
        self.close_connection = True


# -------------------------------------------------------------- entry point
def _make_server(
    port: int, upstream: str, state_dir: Path, workspace_id: str = ""
) -> _ProxyServer:
    if "://" not in upstream:
        upstream = "https://" + upstream
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    server = _ProxyServer(("127.0.0.1", port), _RelayHandler)  # loopback ONLY
    server.ctx_upstream = urlsplit(upstream)
    server.ctx_observer = _Observer(state_dir, workspace_id)
    server.ctx_ssl = ssl.create_default_context()
    server.ctx_pool = []
    server.ctx_pool_lock = threading.Lock()
    return server


def serve_proxy(port: int, upstream: str, state_dir: Path, workspace_id: str = "") -> None:
    """Run the observer proxy in the foreground until SIGINT."""
    server = _make_server(port, upstream, state_dir, workspace_id)
    host, bound_port = server.server_address[:2]
    print(f"ctx proxy: listening on {host}:{bound_port} -> {upstream}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
