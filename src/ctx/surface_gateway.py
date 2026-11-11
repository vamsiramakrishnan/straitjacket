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

    def __init__(self, name: str, argv: list[str], *, timeout: float = 15.0,
                 env: dict[str, str] | None = None, cwd: str | None = None):
        self.name = name
        self.argv = argv
        self.timeout = timeout
        # The environment and working directory the backend was CONFIGURED
        # with. Values live only in this process's memory and the child's
        # environment; they are never logged, rendered or written.
        self._env = env
        self.cwd = cwd
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
                env=self._env, cwd=self.cwd or None,
            )
        except (OSError, ValueError):
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
    """True if this argv is the gateway itself (avoid fronting ourselves),
    however ctx is invoked (``ctx …`` or ``python -P -m ctx …``)."""
    return any(argv[i:i + 2] == ["surface", "gateway"] for i in range(1, len(argv) - 1))


def _snapshot_specs(path: Path) -> dict[str, surface.ServerSpec] | None:
    """Backends named by a snapshot file, or None when it is absent/unreadable.
    A snapshot written by ctx carries NO environment values — at most the
    names in ``env_keys`` — so it is safe at the permissions of any workspace
    file; values are rehydrated from the live config at spawn time."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    out: dict[str, surface.ServerSpec] = {}
    for name, cfg in ((doc.get("mcpServers") if isinstance(doc, dict) else None) or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("command"):
            continue  # remote servers are not proxied by a stdio gateway
        spec = surface._spec(str(name), cfg, host="ctx", scope=str(cfg.get("scope") or "snapshot"),
                             source=str(path))
        if spec is not None:
            out[spec.name] = spec
    return out


def gateway_backend_specs(ws_root: Path | str, backends_file: Path | str | None = None
                          ) -> dict[str, surface.ServerSpec]:
    """Backend servers the gateway fronts, with the environment and working
    directory each was configured with.

    Prefer a snapshot (``backends_file``, else ``.ctx-surface/backends.json``)
    so the host can load ONLY the gateway while the gateway still finds the
    backends; fall back to live config discovery. A snapshot entry whose
    command matches a live config entry of the same name inherits that
    entry's ``env`` and ``cwd`` (values are read from the user's own config
    file at spawn time, never copied). Remote servers are never fronted. The
    gateway never fronts itself."""
    root = Path(ws_root)
    live: dict[str, surface.ServerSpec] = {}
    for s in surface.mcp_server_specs(root):
        if s.transport == "stdio" and s.name not in live:
            live[s.name] = s
    snap = _snapshot_specs(Path(backends_file) if backends_file else root / BACKENDS_FILE)
    if snap is None:
        # Without a snapshot only WORKSPACE servers are fronted: a user- or
        # local-scope server is started only when a snapshot names it.
        specs = {n: s for n, s in live.items() if s.scope == "workspace"}
    else:
        specs = {}
        for name, s in snap.items():
            src = live.get(name)
            if src is not None and src.command == s.command:
                s.env = {**src.env, **s.env}
                s.cwd = s.cwd or src.cwd
            specs[name] = s
    return {n: s for n, s in specs.items() if not _is_gateway_argv(s.argv)}


def gateway_backends(ws_root: Path | str, backends_file: Path | str | None = None
                     ) -> dict[str, list[str]]:
    """Backend name → argv (see :func:`gateway_backend_specs`)."""
    return {n: s.argv for n, s in gateway_backend_specs(ws_root, backends_file).items()}


def _server_family(name: str, argv: list[str]) -> str:
    cap = surface.Capability(id=f"mcp.{name}", kind="mcp_server", provider=name,
                             source="", tokens=0, detail=" ".join(argv))
    return surface.family_of(cap)


def _state_doc(ws_root: Path | str) -> dict[str, Any]:
    try:
        doc = json.loads((Path(ws_root) / STATE_FILE).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def load_state(ws_root: Path | str) -> set[str]:
    """Revealed families, persisted. Kernel families are always included."""
    revealed = set(KERNEL_FAMILIES)
    for f in _state_doc(ws_root).get("revealed", []) or []:
        revealed.add(str(f))
    return revealed


def save_state(ws_root: Path | str, revealed: set[str], *,
               servers: set[str] | None = None,
               tools: set[tuple[str, str]] | None = None) -> None:
    """Persist revealed families (and, when given, servers/tools). Keys not
    passed are preserved, so a family-only writer (reconcile) never drops a
    finer-grained reveal."""
    try:
        doc = _state_doc(ws_root)
        doc["revealed"] = sorted(revealed)
        if servers is not None:
            doc["servers"] = sorted(servers)
        if tools is not None:
            doc["tools"] = sorted([list(t) for t in tools])
        doc = {k: v for k, v in doc.items() if v or k == "revealed"}
        path = Path(ws_root) / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        pass


# ------------------------------------------------------------ gateway core
# The gateway's own schemas are the fixed cost it charges every turn, so new
# behaviour rides the existing three tools: `query` on the index, `target`
# (a family, a server, or one tool) on reveal/hide. No fourth tool.
_TARGET = {"type": "string", "description": "family, server, or mcp__server__tool"}
_META_TOOLS = [
    {
        "name": "surface_index",
        "description": "List capability families and which are revealed. The "
                       "compact directory of what this session can do. With "
                       "query: find matching tools without revealing them.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "surface_reveal",
        "description": "Reveal a capability family so its tools become callable "
                       "(e.g. remote-source-control, testing, deployment), or one "
                       "server or tool.",
        "inputSchema": {"type": "object", "properties": {
            "family": {"type": "string"}, "target": _TARGET}},
    },
    {
        "name": "surface_hide",
        "description": "Hide a capability family's tools again once its phase is done.",
        "inputSchema": {"type": "object", "properties": {
            "family": {"type": "string"}, "target": _TARGET}},
    },
]

_MAX_INDEX_MATCHES = 12
_MAX_DESC_CHARS = 120


class Gateway:
    """Pure-ish gateway logic: which tools are visible, how a call routes, and
    the family directory. The serve loop wraps this over stdio.

    ``backends_file`` names an explicit snapshot (as ``ctx wrap`` passes for
    one session); ``persist`` False keeps reveal state in memory only, so an
    ephemeral session leaves nothing behind and inherits nothing."""

    def __init__(self, ws_root: Path | str, *, backends_file: Path | str | None = None,
                 persist: bool = True):
        self.ws_root = Path(ws_root)
        self.persist = persist
        self.specs = gateway_backend_specs(ws_root, backends_file)
        self.commands = {n: s.argv for n, s in self.specs.items()}
        self.family_of = {n: _server_family(n, a) for n, a in self.commands.items()}
        self.excluded = {n: {str(t) for t in (s.raw.get("exclude_tools") or [])}
                         for n, s in self.specs.items()}
        # allow_tools: when a profile restricts a server per tool, ONLY these
        # are ever listed or callable — a tool the server adds later, or one
        # absent from the catalogue the selection was made from, stays hidden.
        self.allowed = {n: {str(t) for t in s.raw["allow_tools"]}
                        for n, s in self.specs.items()
                        if isinstance(s.raw.get("allow_tools"), list)}
        doc = _state_doc(ws_root) if persist else {}
        self.revealed = load_state(ws_root) if persist else set(KERNEL_FAMILIES)
        self.revealed_servers = {str(x) for x in doc.get("servers", []) or []}
        # (server, tool) pairs: never a joined string, since server names may
        # contain dots or underscores.
        self.revealed_tools: set[tuple[str, str]] = {
            (str(x[0]), str(x[1])) for x in doc.get("tools", []) or []
            if isinstance(x, list) and len(x) == 2}
        self._backends: dict[str, MCPBackend] = {}
        self._cache = surface.read_probe_cache(self.ws_root)

    # -- families -------------------------------------------------------
    def families(self) -> dict[str, list[str]]:
        fam: dict[str, list[str]] = {}
        for name, f in sorted(self.family_of.items()):
            fam.setdefault(f, []).append(name)
        return fam

    def _backend(self, name: str) -> MCPBackend:
        if name not in self._backends:
            argv, env, cwd = surface.spawn_params(self.specs[name])
            self._backends[name] = MCPBackend(name, argv, env=env, cwd=cwd)
        return self._backends[name]

    def _permitted(self, server: str, tool: str) -> bool:
        if tool in self.excluded.get(server, set()):
            return False
        allowed = self.allowed.get(server)
        return allowed is None or tool in allowed

    def _tools_of(self, server: str) -> list[dict[str, Any]]:
        return [t for t in self._backend(server).list_tools()
                if self._permitted(server, str(t.get("name", "")))]

    def _visible(self, server: str, tool: str) -> bool:
        if not self._permitted(server, tool):
            return False
        return (self.family_of.get(server) in self.revealed
                or server in self.revealed_servers
                or (server, tool) in self.revealed_tools)

    def _server_open(self, server: str) -> bool:
        return self.family_of.get(server) in self.revealed or server in self.revealed_servers

    # -- visible surface ------------------------------------------------
    def visible_tools(self) -> list[dict[str, Any]]:
        tools = list(_META_TOOLS)
        for name in sorted(self.family_of):
            wanted = {t for s, t in self.revealed_tools if s == name}
            if not self._server_open(name) and not wanted:
                continue
            for t in self._tools_of(name):
                if not self._visible(name, str(t.get("name", ""))):
                    continue
                schema = dict(t)
                schema["name"] = f"mcp__{name}__{t.get('name', '')}"
                tools.append(schema)
        return tools

    # -- routing --------------------------------------------------------
    def _split(self, rest: str, sep: str) -> tuple[str, str] | None:
        """(server, tool) from ``<server><sep><tool>``, matching the LONGEST
        known server name so dotted/underscored names resolve exactly."""
        for server in sorted(self.commands, key=len, reverse=True):
            if rest.startswith(server + sep) and len(rest) > len(server) + len(sep):
                return server, rest[len(server) + len(sep):]
        return None

    def _resolve(self, raw: str) -> tuple[str, Any]:
        """(kind, key) for a reveal/hide target: ('family', f) | ('server', s)
        | ('tool', (server, tool)) | ('unknown', raw)."""
        t = raw.strip()
        if t.startswith("mcp__"):
            pair = self._split(t[len("mcp__"):], "__")
            return ("tool", pair) if pair else ("unknown", t)
        known = set(self.family_of.values()) | set(KERNEL_FAMILIES)
        if t in known:
            return "family", t
        if t in self.commands:
            return "server", t
        pair = self._split(t, ".")
        return ("tool", pair) if pair else ("unknown", t)

    def _save(self) -> None:
        if self.persist:
            save_state(self.ws_root, self.revealed, servers=self.revealed_servers,
                       tools=self.revealed_tools)

    def call(self, name: str, arguments: dict) -> tuple[dict[str, Any], bool]:
        """Return (result, surface_changed). surface_changed=True after a
        reveal/hide so the server emits tools/list_changed."""
        if name == "surface_index":
            query = str((arguments or {}).get("query") or "").strip()
            return (self._query_result(query) if query else self._index_result()), False
        if name in ("surface_reveal", "surface_hide"):
            raw = str(arguments.get("target") or arguments.get("family") or "").strip()
            kind, key = self._resolve(raw)
            reveal = name == "surface_reveal"
            if kind == "unknown":
                known = set(self.family_of.values()) | set(KERNEL_FAMILIES)
                return (self._text(f"unknown family {raw!r}; available: "
                                   f"{', '.join(sorted(known))} (or a server: "
                                   f"{', '.join(sorted(self.commands)) or 'none'})"), False)
            if kind == "family":
                fam = key
                if reveal:
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
                self._save()
                servers = self.families().get(fam, [])
                return (self._text(f"{'revealed' if reveal else 'hid'} "
                                   f"{fam}: {', '.join(servers) or '(no servers)'}"), True)
            pool = self.revealed_servers if kind == "server" else self.revealed_tools
            server = key if kind == "server" else key[0]
            label = key if kind == "server" else f"mcp__{key[0]}__{key[1]}"
            if reveal:
                if key in pool:
                    return self._text(f"{kind} {label} already revealed"), False
                if kind == "tool" and not self._permitted(server, key[1]):
                    return self._text(f"{label} is excluded by this session's profile"), False
                pool.add(key)
            else:
                if key not in pool:
                    via = self.family_of.get(server)
                    note = f" (visible via family {via})" if via in self.revealed else ""
                    return self._text(f"{kind} {label} not individually revealed{note}"), False
                pool.discard(key)
            self._save()
            return self._text(f"{'revealed' if reveal else 'hid'} {kind} {label}"), True
        # proxied backend tool: mcp__<server>__<tool>
        if name.startswith("mcp__"):
            pair = self._split(name[len("mcp__"):], "__")
            if pair is None:
                return self._text(f"no backend for {name!r}"), False
            server, tool = pair
            if not self._permitted(server, tool):
                return self._text(f"mcp__{server}__{tool} is excluded by this session's "
                                  "profile and cannot be called"), False
            if not self._visible(server, tool):
                return self._text(f"{server} is hidden; surface_reveal "
                                  f"'{self.family_of.get(server)}' first"), False
            return _bound_result(self._backend(server).call(tool, arguments)), False
        return self._text(f"unknown tool {name!r}"), False

    def _index_result(self) -> dict[str, Any]:
        lines = ["Capability families (surface_reveal <family> to enable):"]
        for fam, servers in sorted(self.families().items()):
            mark = "●" if fam in self.revealed else "○"
            lines.append(f"  {mark} {fam}: {', '.join(servers)}")
        if self.revealed_servers or self.revealed_tools:
            lines.append("  also revealed: " + ", ".join(
                sorted(self.revealed_servers)
                + [f"mcp__{s_}__{t_}" for s_, t_ in sorted(self.revealed_tools)]))
        lines.append("● revealed  ○ available · surface_index query=<words> finds tools")
        return self._text("\n".join(lines))

    def _query_result(self, query: str) -> dict[str, Any]:
        """Tools whose name or description match the query, ranked by term
        hits, WITHOUT revealing them. Lists each backend's tools (spawning it
        lazily); bounded to :data:`_MAX_INDEX_MATCHES` lines with the rest
        declared as omitted."""
        terms = [t for t in query.lower().replace("_", " ").split() if t]
        hits: list[tuple[int, str, str, bool]] = []
        for server in sorted(self.commands):
            for t in self._catalog(server):
                tool = str(t.get("name", ""))
                desc = " ".join(str(t.get("description") or t.get("summary") or "").split())
                blob = f"{server} {tool} {desc}".lower().replace("_", " ")
                score = sum(blob.count(term) for term in terms)
                if score:
                    hits.append((score, f"mcp__{server}__{tool}", desc, self._visible(server, tool)))
        hits.sort(key=lambda h: (-h[0], h[1]))
        if not hits:
            return self._text(f"no tool matches {query!r}; surface_index lists families")
        lines = [f"Tools matching {query!r} (surface_reveal target=<name> to enable one):"]
        for _, name, desc, shown in hits[:_MAX_INDEX_MATCHES]:
            d = desc[:_MAX_DESC_CHARS] + ("…" if len(desc) > _MAX_DESC_CHARS else "")
            lines.append(f"  {'●' if shown else '○'} {name} — {d}".rstrip(" —"))
        if len(hits) > _MAX_INDEX_MATCHES:
            lines.append(f"  … {len(hits) - _MAX_INDEX_MATCHES} more matches omitted; "
                         "narrow the query")
        return self._text("\n".join(lines))

    def _catalog(self, server: str) -> list[dict[str, Any]]:
        """Tool names + descriptions for a query: from a live connection if
        one is already open, else from the probe cache, else (only then) by
        starting the backend, bounded by its RPC deadline."""
        if server in self._backends:
            return self._tools_of(server)
        entry = self._cache.get(server)
        if (isinstance(entry, dict) and entry.get("tools")
                and entry.get("key") == surface._probe_cache_key(self.commands[server])):
            return [t for t in entry["tools"] if isinstance(t, dict)
                    and self._permitted(server, str(t.get("name", "")))]
        return self._tools_of(server)

    @staticmethod
    def _text(msg: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": msg}]}

    def close(self) -> None:
        for b in self._backends.values():
            b.close()


# ------------------------------------------------------------ serve loop
def serve_gateway(ws_root: Path | str | None = None, *,
                  backends_file: Path | str | None = None) -> int:
    """Run the gateway as a stdio MCP server until EOF. With an explicit
    ``backends_file`` (a per-session snapshot from ``ctx wrap``) reveal state
    stays in memory: the session leaves no state file behind."""
    import os

    gw = Gateway(ws_root or os.getcwd(), backends_file=backends_file,
                 persist=backends_file is None)
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
           "KERNEL_FAMILIES", "STATE_FILE", "gateway_backends", "gateway_backend_specs"]
