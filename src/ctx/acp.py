"""Bounded ACP v1 stdio worker transport. No provider SDK or agent loop.

Endpoints are explicit argv arrays. Permission requests are denied unless the
user selected allow_once in setup; that choice never changes agent settings.
Only session messaging is advertised: tools still run in the agent, with ctx
available through a session-scoped MCP server and native hook integrations.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

CONFIG = ".ctx/acp.json"
DEFAULT_COMMANDS = {
    "claude": ["claude-agent-acp"],
    "codex": ["codex-acp"],
    "antigravity": ["agy_acp_server.par", "--uid="],
    "hermes": ["hermes", "acp"],
    "omp": ["omp", "acp"],
    "opencode": ["opencode", "acp"],
    "dsh": ["dsh", "--profile", "acp"],
}
MAX_FRAME = 2 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024


class ACPError(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    command: tuple[str, ...]
    model: str
    tier: str = "standard"
    permissions: str = "deny"
    workspace: str = ""


def settings(root: Path) -> dict[str, Endpoint]:
    path = root / CONFIG
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if raw.get("version") != 1 or not isinstance(raw.get("hosts"), dict):
            raise ValueError("expected version 1 and hosts object")
        endpoints = {}
        for host, value in raw["hosts"].items():
            command = value.get("command")
            model = value.get("model")
            tier = value.get("tier", "standard")
            permissions = value.get("permissions", "deny")
            if (host not in DEFAULT_COMMANDS or not isinstance(command, list) or not command
                    or not all(isinstance(a, str) and a and "\0" not in a for a in command)
                    or not isinstance(model, str) or not model.strip()
                    or tier not in ("economy", "standard", "frontier")
                    or permissions not in ("deny", "allow_once")):
                raise ValueError(f"invalid endpoint for {host}")
            endpoints[host] = Endpoint(tuple(command), model, tier, permissions, str(root.resolve()))
        return endpoints
    except (OSError, TypeError, AttributeError, ValueError) as exc:
        raise ACPError(f"Invalid {CONFIG}: {exc}") from exc


def configure(root: Path, host: str, model: str, *, command: list[str] | None = None,
              tier: str = "standard", permissions: str = "deny") -> str:
    from ctx.mcp_hosts import _write

    if host not in DEFAULT_COMMANDS or not model:
        raise ACPError("ACP setup requires one supported --host and --acp-model")
    path = root / CONFIG
    if path.is_symlink() or path.parent.is_symlink():
        raise ACPError(f"Refusing symlink: {CONFIG}")
    settings(root)  # refuse malformed existing configuration
    raw = json.loads(path.read_text()) if path.exists() else {"version": 1, "hosts": {}}
    argv = command or DEFAULT_COMMANDS[host]
    if not argv or not shutil.which(argv[0]):
        raise ACPError(f"ACP executable not found: {argv[0] if argv else '(empty)'}. Install the adapter or pass --acp-command as a JSON argv array.")
    raw["hosts"][host] = {"command": argv, "model": model, "tier": tier, "permissions": permissions}
    _write(path, raw)
    return f"{host}: ACP configured in {CONFIG}; model {model}; permission requests: {permissions}. Run ctx doctor for configuration checks."


def configured_hosts(hosts, root: Path, *, which=shutil.which):
    """Overlay only configured endpoints; never invent a provider's model list."""
    from ctx.hosts import ModelChoice
    from ctx.pricing import price_for

    endpoints = settings(root)
    result = []
    for host in hosts:
        endpoint = endpoints.get(host.spec.name)
        if endpoint is None:
            result.append(host)
            continue
        path = which(endpoint.command[0])
        spec = replace(host.spec, unattended=True, default_model=endpoint.model,
                       coordinator_model=endpoint.model,
                       models=(ModelChoice(endpoint.model, endpoint.tier),))
        result.append(replace(host, spec=spec, installed=bool(path), path=path,
                              model=endpoint.model, price=price_for(endpoint.model, workspace_root=root),
                              acp=endpoint))
    return result


def checks(root: Path):
    try:
        return [(f"acp.{host}", bool(shutil.which(ep.command[0])),
                 f"{ep.command[0]}; model {ep.model}; permissions {ep.permissions}; configuration only, no live handshake")
                for host, ep in settings(root).items()]
    except ACPError as exc:
        return [("acp.config", False, str(exc))]


class Client:
    def __init__(self, command, cwd, *, timeout, idle_timeout=0, env=None, permissions="deny"):
        self.command = command
        self.deadline = time.monotonic() + timeout
        self.last_activity = time.monotonic()
        self.idle_timeout = idle_timeout
        self.permissions = permissions
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)
        self.events = queue.Queue(maxsize=128)
        self.stop = threading.Event()
        self.stderr = bytearray()
        self.output = []
        self.output_bytes = 0
        self.session = None
        self.serial = 0
        self.denied = 0
        self.threads = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._read_err, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.events.put(value, timeout=.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self.stop.is_set():
                line = self.proc.stdout.readline(MAX_FRAME + 1)
                if not line:
                    raise ACPError("ACP agent closed stdout before completing the request")
                self.last_activity = time.monotonic()
                if len(line) > MAX_FRAME:
                    raise ACPError("ACP frame exceeds 2 MiB")
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    raise ACPError("Invalid ACP JSON-RPC frame")
                self._put(value)
        except (OSError, ValueError) as exc:
            self._put(ACPError(str(exc)))

    def _read_err(self):
        while not self.stop.is_set():
            data = self.proc.stderr.read1(4096)
            if not data:
                return
            # Stderr logs do not count as protocol progress.
            self.stderr.extend(data[:max(0, 65536 - len(self.stderr))])

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ACPError("ACP wall timeout")
        if self.idle_timeout and time.monotonic() - self.last_activity >= self.idle_timeout:
            raise ACPError("ACP idle timeout")
        return min(remaining, .1)

    def send(self, message):
        data = (json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode()
        if len(data) > MAX_FRAME:
            raise ACPError("ACP outgoing frame exceeds 2 MiB")
        done = threading.Event()
        errors = []
        def write():
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except OSError as exc:
                errors.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        while not done.wait(self._remaining()):
            pass
        if errors:
            raise ACPError("ACP agent closed stdin") from errors[0]

    def request(self, method, params):
        self.serial += 1
        rid = self.serial
        self.send({"id": rid, "method": method, "params": params})
        while True:
            try:
                message = self.events.get(timeout=self._remaining())
            except queue.Empty:
                continue
            if isinstance(message, Exception):
                raise message
            if "method" in message:
                self._incoming(message)
            elif message.get("id") == rid:
                if "error" in message:
                    error = message["error"]
                    raise ACPError(f"ACP {method}: {str(error)[:2000]}")
                if "result" not in message:
                    raise ACPError("ACP response has no result")
                return message["result"]
            else:
                raise ACPError("Unexpected ACP response id")

    def _incoming(self, message):
        method = message["method"]
        params = message.get("params") or {}
        if "id" in message:
            if method == "session/request_permission" and params.get("sessionId") == self.session:
                wanted = "allow_once" if self.permissions == "allow_once" else "reject_once"
                option = next((o for o in params.get("options", []) if o.get("kind") == wanted), None)
                outcome = ({"outcome": "selected", "optionId": option["optionId"]}
                           if option else {"outcome": "cancelled"})
                if wanted != "allow_once" or option is None:
                    self.denied += 1
                self.send({"id": message["id"], "result": {"outcome": outcome}})
            else:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Client capability not supported"}})
        elif method == "session/update" and params.get("sessionId") == self.session:
            update = params.get("update") or {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    text = content.get("text", "")
                    self.output_bytes += len(text.encode())
                    if self.output_bytes > MAX_OUTPUT:
                        raise ACPError("ACP response exceeds 2 MiB; worker result refused")
                    self.output.append(text)

    def open(self, cwd, exe):
        from ctx.mcp_hosts import ctx_argv

        initialized = self.request("initialize", {"protocolVersion": 1,
            "clientCapabilities": {}, "clientInfo": {"name": "straitjacket", "version": "1"}})
        if not isinstance(initialized, dict) or initialized.get("protocolVersion") != 1:
            raise ACPError("ACP agent did not negotiate protocol v1")
        argv = ctx_argv(exe)
        command = shutil.which(argv[0])
        if not command:
            raise ACPError("ctx MCP executable not found")
        session = self.request("session/new", {"cwd": str(Path(cwd).resolve()), "mcpServers": [{
            "name": "ctx-harness", "command": command,
            "args": [*argv[1:], "mcp", "--bounded-only", "--with-edits", "--workspace", str(Path(cwd).resolve())], "env": []}]})
        if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
            raise ACPError("ACP session/new did not return a session id")
        self.session = session["sessionId"]
        return session

    def select_model(self, session, model):
        # Prefer stable config options. Older agents expose models/set_model.
        for option in session.get("configOptions", []):
            if option.get("category") != "model":
                continue
            def values(options):
                for item in options:
                    if "options" in item:
                        yield from values(item["options"])
                    elif "value" in item:
                        yield item["value"]
            if model not in set(values(option.get("options", []))):
                raise ACPError(f"ACP agent does not advertise model {model}")
            if option.get("currentValue") != model:
                self.request("session/set_config_option", {"sessionId": self.session, "configId": option["id"], "value": model})
            return
        models = session.get("models") or {}
        if model not in {m.get("modelId") for m in models.get("availableModels", [])}:
            raise ACPError(f"ACP agent does not advertise model {model}; use its exact model id")
        if models.get("currentModelId") != model:
            self.request("session/set_model", {"sessionId": self.session, "modelId": model})

    def close(self):
        # Best-effort cancel followed by unconditional group cleanup. A small
        # grace window lets cooperative agents release their own resources.
        if self.session and self.proc.poll() is None:
            self.deadline = time.monotonic() + .2
            self.idle_timeout = 0
            try:
                self.send({"method": "session/cancel", "params": {"sessionId": self.session}})
            except (ACPError, OSError):
                pass
        self.stop.set()
        from ctx._proc import kill_and_reap
        kill_and_reap(self.proc)
        for thread in self.threads:
            thread.join(timeout=.5)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()


def launch(endpoint: Endpoint, root: Path, prompt: str, exe: str, *, timeout: float,
           idle_timeout: float = 0, env=None):
    client = None
    try:
        client = Client(endpoint.command, root, timeout=timeout, idle_timeout=idle_timeout,
                        env=env, permissions=endpoint.permissions)
        session = client.open(root, exe)
        client.select_model(session, endpoint.model)
        result = client.request("session/prompt", {"sessionId": client.session,
                                "prompt": [{"type": "text", "text": prompt}]})
        reason = result.get("stopReason") if isinstance(result, dict) else None
        if reason != "end_turn":
            raise ACPError(f"ACP prompt stopped: {reason}")
        if client.denied:
            raise ACPError(f"ACP worker encountered {client.denied} unresolved permission request(s)")
        return 0, "".join(client.output), client.stderr.decode(errors="replace"), None
    except (OSError, ACPError, TypeError, AttributeError, KeyError) as exc:
        return 2, "".join(client.output) if client else "", str(exc), None
    finally:
        if client:
            client.close()
