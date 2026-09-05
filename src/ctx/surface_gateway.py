"""Phase 4 — progressive-disclosure MCP gateway.

A single stdio MCP server that fronts the workspace's backend MCP servers and
exposes, by default, only a **compact capability index** plus ``surface_reveal``
/ ``surface_hide`` meta-tools. Backend tools become visible only when their
family is revealed; revealing emits ``notifications/tools/list_changed`` so a
client that honours it (Claude Code's normal path) refreshes in-session.

    large tool surface → compact capability index → reveal what the task earns

Honesty about enforcement (docs/CAPABILITY-SURFACE.md §hosts): dynamic reveal
lands in-session only where the client re-fetches on ``list_changed``. On
startup-only clients (Codex; Antigravity without a manual refresh) the gateway
still works as one bounded entry point, but newly revealed tools appear after a
reconnect/refresh — the *enforced* minimum there is the Phase 3 compiled
config. The gateway is the affordance; compile is the boundary.

Backend connections are lazy (spawned on first reveal) and kept alive for the
session; tool calls are proxied over the persisted stdio connection and
correlated by JSON-RPC id. Every path fails soft — a broken backend yields a
tool error, never a gateway crash.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ctx import surface

PROTOCOL_VERSION = "2024-11-05"
STATE_FILE = ".ctx-surface/gateway-state.json"
KERNEL_FAMILIES = frozenset({"harness"})  # always revealed
_MAX_BACKEND_RESULT_BYTES = 16384  # cap proxied backend output (input+output containment)


def _bound_result(result: dict[str, Any], max_bytes: int = _MAX_BACKEND_RESULT_BYTES) -> dict[str, Any]:
    """The gateway is the input-side boundary, but a proxied backend can still
    return a flood — so bound its text content here, composing capability
    containment with output containment. An oversized text block is truncated
    with an honest note; structured/binary blocks pass through."""
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return result
    out_blocks = []
    for block in result["content"]:
        if (isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)):
            raw = block["text"].encode("utf-8")
            if len(raw) > max_bytes:
                # The note is part of the output, so it comes OUT of the
                # budget rather than being added on top of it. Appending it
                # to a slice already at max_bytes made the block that
                # announces a 16,384-byte cap the thing that exceeded it --
                # a bound whose own disclosure broke it.
                note = (
                    f"\n[ctx-gateway: backend output bounded to {max_bytes:,} of "
                    f"{len(raw):,} bytes — the gateway caps proxied floods]"
                )
                room = max(0, max_bytes - len(note.encode("utf-8")))
                head = raw[:room].decode("utf-8", "ignore")
                block = {**block, "text": head + note}
        out_blocks.append(block)
    return {**result, "content": out_blocks}


# ------------------------------------------------------------ backend client
class MCPBackend:
    """A persistent stdio connection to one backend MCP server. Lazy: the
    subprocess is spawned on first use and reused for the session."""

    def __init__(self, name: str, argv: list[str], *, timeout: float = 15.0):
        self.name = name
        self.argv = argv
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._tools: list[dict[str, Any]] | None = None
        self._rbuf = b""

    def _ensure(self) -> subprocess.Popen | None:
        if self._proc and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError:
            self._proc = None
            return None
        self._id = 0
        self._rbuf = b""
        # MCP handshake: initialize request, then the initialized notification.
        if self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "ctx-gateway", "version": "1"}}) is None:
            self.close()
            self._proc = None
            return None
        self._notify("notifications/initialized")
        return self._proc

    def _notify(self, method: str) -> None:
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _rpc(self, method: str, params: dict) -> dict | None:
        """Send a request and read until the matching id response, bounded by a
        wall-clock deadline so a hung backend cannot block the gateway. Skips
        interleaved notifications/other-id messages. Best-effort → None."""
        import select
        import time

        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None
        self._id += 1
        rid = self._id
        try:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "method": method, "params": params}) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return None
        # Read raw chunks under the deadline and split lines ourselves.
        # `select` then `readline()` only bounded the FIRST byte: a backend
        # that wrote half a line and hung -- the case the deadline exists for
        # -- left readline blocking for the newline with no timeout at all.
        # All reads go through this buffer, never the TextIOWrapper's.
        import os

        fd = proc.stdout.fileno()
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self._rbuf:
                raw, self._rbuf = self._rbuf.split(b"\n", 1)
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == rid:
                    return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                return None
            if not ready:
                return None  # timed out waiting for the backend
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return None
            if not chunk:
                return None  # backend closed its stdout
            self._rbuf += chunk

    def list_tools(self) -> list[dict[str, Any]]:
        if self._tools is not None:
            return self._tools
        if self._ensure() is None:
            return []
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):  # bound the page count defensively
            params = {"cursor": cursor} if cursor else {}
            resp = self._rpc("tools/list", params)
            result = (resp or {}).get("result", {}) if resp else {}
            tools.extend(t for t in result.get("tools", []) if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self._tools = tools
        return self._tools

    def call(self, tool: str, arguments: dict) -> dict[str, Any]:
        if self._ensure() is None:
            return {"content": [{"type": "text", "text": f"backend {self.name} unavailable"}],
                    "isError": True}
        resp = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if resp is None or "result" not in resp:
            err = (resp or {}).get("error", {}).get("message", "no response")
            return {"content": [{"type": "text", "text": f"{self.name}.{tool}: {err}"}],
                    "isError": True}
        return resp["result"]

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass


# ------------------------------------------------------------ gateway state
BACKENDS_FILE = ".ctx-surface/backends.json"


def _is_gateway_argv(argv: list[str]) -> bool:
    """True if this argv is the gateway itself (avoid fronting ourselves)."""
    return len(argv) >= 3 and argv[1:3] == ["surface", "gateway"]


def gateway_backends(ws_root: Path | str) -> dict[str, list[str]]:
    """Backend servers the gateway fronts. Prefer the stable snapshot written
    at install time (``.ctx-surface/backends.json``) so the host can load ONLY
    the gateway while the gateway still finds the backends; fall back to live
    config discovery. The gateway never fronts itself."""
    root = Path(ws_root)
    servers: dict[str, list[str]] = {}
    try:
        doc = json.loads((root / BACKENDS_FILE).read_text(encoding="utf-8"))
        for name, cfg in (doc.get("mcpServers") or {}).items():
            if isinstance(cfg, dict) and cfg.get("command"):
                servers[str(name)] = [str(cfg["command"])] + [str(a) for a in cfg.get("args", [])]
    except Exception:
        servers = dict(surface._mcp_server_commands(ws_root))
    return {n: a for n, a in servers.items() if not _is_gateway_argv(a)}


def _server_family(name: str, argv: list[str]) -> str:
    cap = surface.Capability(id=f"mcp.{name}", kind="mcp_server", provider=name,
                             source="", tokens=0, detail=" ".join(argv))
    return surface.family_of(cap)


def load_state(ws_root: Path | str) -> set[str]:
    """Revealed families, persisted. Kernel families are always included."""
    revealed = set(KERNEL_FAMILIES)
    try:
        doc = json.loads((Path(ws_root) / STATE_FILE).read_text(encoding="utf-8"))
        for f in doc.get("revealed", []):
            revealed.add(str(f))
    except Exception:
        pass
    return revealed


def save_state(ws_root: Path | str, revealed: set[str]) -> None:
    try:
        path = Path(ws_root) / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"revealed": sorted(revealed)}, indent=2) + "\n",
                        encoding="utf-8")
    except Exception:
        pass


# ------------------------------------------------------------ gateway core
_META_TOOLS = [
    {
        "name": "surface_index",
        "description": "List capability families and which are revealed. The "
                       "compact directory of what this session can do.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "surface_reveal",
        "description": "Reveal a capability family so its tools become callable "
                       "(e.g. remote-source-control, testing, deployment).",
        "inputSchema": {"type": "object", "properties": {
            "family": {"type": "string"}}, "required": ["family"]},
    },
    {
        "name": "surface_hide",
        "description": "Hide a capability family's tools again once its phase is done.",
        "inputSchema": {"type": "object", "properties": {
            "family": {"type": "string"}}, "required": ["family"]},
    },
]


class Gateway:
    """Pure-ish gateway logic: which tools are visible, how a call routes, and
    the family directory. The serve loop wraps this over stdio."""

    def __init__(self, ws_root: Path | str):
        self.ws_root = Path(ws_root)
        self.commands = gateway_backends(ws_root)
        self.family_of = {n: _server_family(n, a) for n, a in self.commands.items()}
        self.revealed = load_state(ws_root)
        self._backends: dict[str, MCPBackend] = {}

    # -- families -------------------------------------------------------
    def families(self) -> dict[str, list[str]]:
        fam: dict[str, list[str]] = {}
        for name, f in sorted(self.family_of.items()):
            fam.setdefault(f, []).append(name)
        return fam

    def _backend(self, name: str) -> MCPBackend:
        if name not in self._backends:
            self._backends[name] = MCPBackend(name, self.commands[name])
        return self._backends[name]

    # -- visible surface ------------------------------------------------
    def visible_tools(self) -> list[dict[str, Any]]:
        tools = list(_META_TOOLS)
        for name, fam in sorted(self.family_of.items()):
            if fam not in self.revealed:
                continue
            for t in self._backend(name).list_tools():
                schema = dict(t)
                schema["name"] = f"mcp__{name}__{t.get('name', '')}"
                tools.append(schema)
        return tools

    # -- routing --------------------------------------------------------
    def call(self, name: str, arguments: dict) -> tuple[dict[str, Any], bool]:
        """Return (result, surface_changed). surface_changed=True after a
        reveal/hide so the server emits tools/list_changed."""
        if name == "surface_index":
            return self._index_result(), False
        if name in ("surface_reveal", "surface_hide"):
            fam = str(arguments.get("family", "")).strip()
            known = set(self.family_of.values()) | set(KERNEL_FAMILIES)
            if fam not in known:
                return (self._text(f"unknown family {fam!r}; available: "
                                   f"{', '.join(sorted(known))}"), False)
            if name == "surface_reveal":
                if fam in KERNEL_FAMILIES or fam in self.revealed:
                    return self._text(f"family {fam} already revealed"), False
                self.revealed.add(fam)
            else:
                if fam in KERNEL_FAMILIES:
                    return self._text(f"family {fam} is kernel; cannot hide"), False
                if fam not in self.revealed:
                    # Mirror of the reveal guard: a no-op must not report
                    # surface_changed, or the server emits a spurious
                    # tools/list_changed and the caller reads "hid" for
                    # something that was never shown.
                    return self._text(f"family {fam} already hidden"), False
                self.revealed.discard(fam)
            save_state(self.ws_root, self.revealed)
            servers = self.families().get(fam, [])
            return (self._text(f"{'revealed' if name == 'surface_reveal' else 'hid'} "
                               f"{fam}: {', '.join(servers) or '(no servers)'}"), True)
        # proxied backend tool: mcp__<server>__<tool>
        if name.startswith("mcp__"):
            _, _, rest = name.partition("mcp__")
            server, _, tool = rest.partition("__")
            if server not in self.commands:
                return self._text(f"no backend {server!r}"), False
            if self.family_of.get(server) not in self.revealed:
                return self._text(f"{server} is hidden; surface_reveal "
                                  f"'{self.family_of.get(server)}' first"), False
            return _bound_result(self._backend(server).call(tool, arguments)), False
        return self._text(f"unknown tool {name!r}"), False

    def _index_result(self) -> dict[str, Any]:
        lines = ["Capability families (surface_reveal <family> to enable):"]
        for fam, servers in sorted(self.families().items()):
            mark = "●" if fam in self.revealed else "○"
            lines.append(f"  {mark} {fam}: {', '.join(servers)}")
        lines.append("● revealed  ○ available")
        return self._text("\n".join(lines))

    @staticmethod
    def _text(msg: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": msg}]}

    def close(self) -> None:
        for b in self._backends.values():
            b.close()


# ------------------------------------------------------------ serve loop
def serve_gateway(ws_root: Path | str | None = None) -> int:
    """Run the gateway as a stdio MCP server until EOF."""
    import os

    gw = Gateway(ws_root or os.getcwd())
    out = sys.stdout

    def reply(msg_id: Any, result: Any = None, error: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        out.write(json.dumps(msg) + "\n")
        out.flush()

    def notify(method: str) -> None:
        out.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        out.flush()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue  # e.g. a JSON array/batch: msg.get below would raise AttributeError
            method, msg_id = msg.get("method"), msg.get("id")
            if method == "initialize":
                reply(msg_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "ctx-surface-gateway", "version": "1"}})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                reply(msg_id, {"tools": gw.visible_tools()})
            elif method == "tools/call":
                params = msg.get("params") or {}
                result, changed = gw.call(str(params.get("name", "")),
                                          params.get("arguments") or {})
                reply(msg_id, result)
                if changed:
                    notify("notifications/tools/list_changed")
            elif method == "ping":
                reply(msg_id, {})
            elif msg_id is not None:
                reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
    finally:
        gw.close()
    return 0


__all__ = ["Gateway", "MCPBackend", "serve_gateway", "load_state", "save_state",
           "KERNEL_FAMILIES", "STATE_FILE"]
