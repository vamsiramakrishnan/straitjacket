"""Native MCP configuration for the additional coding agents.

Render each host's native MCP schema. JSON merges preserve unrelated settings;
Hermes owns its YAML writes through its CLI, and DSH receives a separate overlay.
No provider, model, credentials, permissions, or native tools are changed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HOSTS = ("hermes", "omp", "opencode", "dsh")
FILES = {
    "hermes": ".ctx/hosts/hermes.json",
    "omp": ".omp/mcp.json",
    "opencode": "opencode.json",
    "dsh": ".ctx/hosts/dsh.cordis.patch.yml",
}
SERVER = "ctx-harness"


class IntegrationError(ValueError):
    """Setup could not establish the requested native configuration."""


def ctx_argv(exe: str | None = None) -> list[str]:
    if exe is not None:
        argv = shlex.split(exe)
    else:
        binary = shutil.which("ctx")
        argv = [binary] if binary else [sys.executable, "-m", "ctx"]
    if not argv:
        raise IntegrationError("ctx executable command is empty")
    return argv


def configuration(host: str, root: Path, exe: str | None = None) -> dict | list:
    argv = ctx_argv(exe)
    # Hermes configuration belongs to the active profile, not one repository.
    workspace = "${workspaceFolder}" if host == "hermes" else str(root.resolve())
    args = [*argv[1:], "mcp", "--bounded-only", "--with-edits", "--workspace", workspace]
    server = {"command": argv[0], "args": args}
    if host == "hermes":
        return {"mcp_servers": {SERVER: server}}
    if host == "omp":
        return {"mcpServers": {SERVER: {"type": "stdio", **server}}}
    if host == "opencode":
        return {"mcp": {SERVER: {"type": "local", "command": [argv[0], *args], "enabled": True}}}
    if host == "dsh":
        # JSON is also YAML; avoid a new YAML dependency and executable tags.
        from ctx.native_hooks import PATHS
        return [{"insert": [{"id": SERVER, "name": "@deepseek-ai/dsh-mcp-client",
                             "config": {"serverName": SERVER, "transport": "stdio", **server}},
                            {"id": "straitjacket-hooks", "name": str(root.resolve() / PATHS[host])}]}]
    raise IntegrationError(f"unknown MCP host: {host}")


def render_config(host: str, root: Path, exe: str | None = None) -> str:
    return json.dumps(configuration(host, root, exe), indent=2) + "\n"


def _read(path: Path) -> dict | list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise IntegrationError(f"Cannot read {path.name} as JSON; preserve it and inspect `ctx wrap <host> --print-config`.") from exc


def _hermes(*args: str) -> subprocess.CompletedProcess:
    binary = shutil.which("hermes")
    if not binary:
        raise IntegrationError("Hermes is not on PATH; install it, then run `ctx setup --host hermes`.")
    try:
        return subprocess.run([binary, "config", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError("Hermes config command failed; check `hermes config path` and retry.") from exc


def _hermes_servers() -> dict:
    result = _hermes("get", "mcp_servers", "--json")
    if result.returncode:
        if "Config key not set: mcp_servers" in result.stdout + result.stderr:
            return {}
        raise IntegrationError("Cannot inspect Hermes MCP configuration; requires `hermes config get mcp_servers --json`.")
    try:
        servers = json.loads(result.stdout)
    except ValueError as exc:
        raise IntegrationError("Hermes config get did not return JSON; update Hermes and retry.") from exc
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise IntegrationError("Hermes mcp_servers must be a mapping; fix it before setup.")
    return servers


def _merged(host: str, root: Path, expected: dict | list) -> dict | list:
    path = root / FILES[host]
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
        raise IntegrationError(f"Refusing to write through a symlink: {FILES[host]}")
    if host == "opencode" and (root / "opencode.jsonc").exists():
        raise IntegrationError("opencode.jsonc exists; merge the `ctx wrap opencode --print-config` mcp entry there. ctx will not shadow or rewrite JSONC.")
    if not path.exists():
        return expected
    current = _read(path)
    if host in ("hermes", "dsh"):
        if current != expected:
            raise IntegrationError(f"{FILES[host]} differs from the generated configuration; move it aside or reconcile it before setup.")
        return current
    key = "mcp" if host == "opencode" else "mcpServers"
    if not isinstance(current, dict) or not isinstance(current.get(key, {}), dict):
        raise IntegrationError(f"{FILES[host]} must contain an object at {key}.")
    servers = current.setdefault(key, {})
    wanted = expected[key][SERVER]
    if SERVER in servers and servers[SERVER] != wanted:
        raise IntegrationError(f"{FILES[host]} already defines {SERVER}; reconcile it with `ctx wrap {host} --print-config` before setup.")
    disabled = current.get("disabledServers", [])
    if not isinstance(disabled, list):
        raise IntegrationError(f"disabledServers must be a list in {FILES[host]}.")
    if SERVER in disabled:
        raise IntegrationError(f"{SERVER} is disabled in {FILES[host]}; enable it before setup.")
    servers[SERVER] = wanted
    return current


def conflicts(host: str, root: Path) -> list[str]:
    from ctx.native_hooks import conflicts as hook_conflicts
    problems = hook_conflicts(host, root)
    if problems:
        return problems
    try:
        expected = configuration(host, root)
        _merged(host, root, expected)
        if host == "hermes" and shutil.which("hermes"):
            existing = _hermes_servers().get(SERVER)
            if existing is not None and existing != expected["mcp_servers"][SERVER]:
                raise IntegrationError("Hermes already defines ctx-harness; reconcile that entry with `ctx wrap hermes --print-config` before setup.")
    except IntegrationError as exc:
        return [str(exc)]
    return []


def _write(path: Path, value: dict | list) -> None:
    text = json.dumps(value, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive temp file; do not leave a truncated user configuration on failure.
    import tempfile

    fd, temporary = tempfile.mkstemp(prefix=".ctx-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(host: str, ws, *, init_policy: bool = True) -> str:
    from ctx.installer import init_workspace

    problems = conflicts(host, ws.root)
    if problems:
        raise IntegrationError("\n".join(problems))
    expected = configuration(host, ws.root)
    merged = _merged(host, ws.root, expected)
    if host == "hermes" and shutil.which("hermes"):
        wanted = expected["mcp_servers"][SERVER]
        if _hermes_servers().get(SERVER) != wanted:
            result = _hermes("set", f"mcp_servers.{SERVER}", json.dumps(wanted))
            if result.returncode or _hermes_servers().get(SERVER) != wanted:
                raise IntegrationError("Hermes did not activate ctx-harness; inspect its active profile with `hermes config path`.")
    _write(ws.root / FILES[host], merged)
    if init_policy:
        init_workspace(ws.root, quiet=True)
    from ctx.native_hooks import install as install_hooks
    hook_note = install_hooks(host, ws.root)
    return (f"{host}: wrote {FILES[host]}\n"
            + hook_note + "\n"
            + next_step(host, ws.root))


def next_step(host: str, root: Path) -> str:
    if host == "hermes":
        return ("Hermes uses the active profile's MCP settings (shared across its sessions).\n"
                "If Hermes was absent, install it and rerun `ctx setup --host hermes`.\n"
                "Start: hermes chat")
    if host == "dsh":
        return "Start: " + shlex.join(["dsh", "--patch", str(root / FILES[host]), "--profile", "web"])
    return f"Start: {host}"


def checks(root: Path) -> list[tuple[str, bool, str]]:
    rows = []
    for host in HOSTS:
        path = root / FILES[host]
        if not path.exists():
            continue
        try:
            expected = configuration(host, root)
            current = _read(path)
            if host == "hermes":
                ok = _hermes_servers().get(SERVER) == expected["mcp_servers"][SERVER]
            elif host == "dsh":
                ok = current == expected
            else:
                key = "mcp" if host == "opencode" else "mcpServers"
                ok = (isinstance(current, dict) and isinstance(current.get(key), dict)
                      and current[key].get(SERVER) == expected[key][SERVER]
                      and SERVER not in current.get("disabledServers", []))
                if host == "opencode" and (root / "opencode.jsonc").exists():
                    ok = False
            detail = "config verified; " + next_step(host, root) if ok else "configuration missing or changed; rerun setup"
        except (IntegrationError, TypeError) as exc:
            ok, detail = False, str(exc)
        label = f"{host} MCP" if host != "dsh" else "dsh MCP overlay (requires --patch)"
        rows.append((label, ok, detail))
    from ctx.native_hooks import checks as hook_checks
    return [*rows, *hook_checks(root)]


def wrap(host: str, root: Path, agent_args: list[str] | None = None) -> int:
    from ctx.workspace import resolve_workspace

    try:
        if host == "hermes" and any(arg in ("--profile", "-p") or arg.startswith("--profile=") for arg in (agent_args or [])):
            raise IntegrationError("Select the Hermes profile before setup (or set HERMES_HOME for setup and launch); a launch-only --profile would configure a different profile.")
        print(install(host, resolve_workspace(str(root))))
        if not agent_args:
            return 0 if all(ok for name, ok, _ in checks(root) if name.startswith(host + " ")) else 1
        binary = shutil.which(host)
        if not binary:
            raise IntegrationError(f"{host} is not on PATH; install it before launching.")
        prefix = [binary]
        if host == "dsh":
            prefix += ["--patch", str(root / FILES[host])]
        return subprocess.run([*prefix, *agent_args], cwd=root).returncode
    except (IntegrationError, OSError) as exc:
        print(f"ctx wrap {host}: {exc}", file=sys.stderr)
        return 2
