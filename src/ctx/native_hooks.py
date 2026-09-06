"""Native plugin rendering and the small host-neutral hook wire contract.

Keep normalization stdlib-only: it runs on the hook fast path. Templates ship
inside the wheel; generated plugins invoke the same ctx guard and edit ledger
as Claude/Codex instead of copying policy into Python/JavaScript plugins.
"""
from __future__ import annotations

import json
from pathlib import Path

HOSTS = ("hermes", "omp", "opencode", "dsh")
PATHS = {
    "omp": ".omp/hooks/pre/straitjacket.js",
    "opencode": ".opencode/plugins/straitjacket.js",
    "dsh": ".ctx/hosts/straitjacket-dsh.mjs",
    "hermes": ".ctx/hosts/hermes-plugin/__init__.py",
}
MARKER = "Straitjacket managed native hooks v1"


def normalize(host: str, payload: dict) -> dict:
    payload = dict(payload)
    name = str(payload.get("tool_name", ""))
    # Explicit native names only; never classify unrelated MCP names by substring.
    if host == "hermes" and name == "terminal":
        payload["tool_name"] = "Bash"
    return payload


def decision_for(host: str, decision: dict) -> dict:
    rewrite = decision.get("rewrite") or {}
    if isinstance(rewrite.get("updatedInput"), dict):
        if host != "dsh":
            return {"action": "rewrite", "input": rewrite["updatedInput"]}
        return {"action": "block", "reason": str(rewrite.get("reason", "Use a bounded ctx operation"))
                + "\nDSH cannot rewrite sealed tool arguments. Retry with: "
                + json.dumps(rewrite["updatedInput"], ensure_ascii=False)[:2000]}
    action = {"allow": "allow", "deny": "block", "force_ask": "ask"}.get(decision.get("decision"), "block")
    return {"action": action, "reason": decision.get("reason", "")}


def render(host: str, exe: str | None = None) -> str:
    from ctx.mcp_hosts import ctx_argv

    data = Path(__file__).parent / "data" / "native-hooks"
    suffix = "py" if host == "hermes" else "mjs"
    source = (data / f"{host}.{suffix}").read_text()
    if host != "hermes":
        source = (data / "bridge.mjs").read_text() + "\n" + source
    return source.replace("__CTX_ARGV__", json.dumps(ctx_argv(exe)))


def _write_owned(path: Path, text: str):
    import os
    import tempfile
    from ctx.mcp_hosts import IntegrationError

    if any(p.is_symlink() for p in (path, *path.parents)):
        raise IntegrationError(f"Refusing symlink for native plugin: {path}")
    if path.exists() and MARKER not in path.read_text():
        raise IntegrationError(f"Existing unmanaged plugin at {path}; move it before setup")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ctx-hook-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def conflicts(host: str, root: Path):
    path = root / PATHS[host]
    if any(p.is_symlink() for p in (path, *path.parents)):
        return [f"Refusing symlink for native plugin: {path}"]
    try:
        if path.exists() and MARKER not in path.read_text():
            return [f"Existing unmanaged plugin at {path}; move it before setup"]
    except (OSError, UnicodeError):
        return [f"Cannot read existing native plugin at {path}"]
    return []


def install(host: str, root: Path) -> str:
    import shutil
    import subprocess
    from ctx.mcp_hosts import IntegrationError, _hermes

    source = render(host)
    path = root / PATHS[host]
    _write_owned(path, source)
    if host == "hermes":
        manifest = f"# {MARKER}\nname: straitjacket\nversion: '1.0'\ndescription: Bounded evidence and tool hooks\n"
        _write_owned(path.parent / "plugin.yaml", manifest)
        if not shutil.which("hermes"):
            return "Hermes plugin prepared; rerun setup after installing Hermes."
        result = _hermes("path")
        profile = Path(result.stdout.strip())
        if result.returncode or not profile.is_absolute() or not profile.is_file():
            raise IntegrationError("Cannot locate Hermes active profile from `hermes config path`")
        plugin = profile.parent / "plugins" / "straitjacket"
        _write_owned(plugin / "__init__.py", source)
        _write_owned(plugin / "plugin.yaml", manifest)
        result = subprocess.run([shutil.which("hermes"), "plugins", "enable", "straitjacket"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise IntegrationError("Hermes plugin prepared but not enabled; run `hermes plugins enable straitjacket` and rerun setup")
        return "Hermes native hooks installed in the active profile (run only in ctx workspaces)."
    return f"{host}: native hooks installed at {PATHS[host]}. Restart existing sessions."


def checks(root: Path):
    from ctx.mcp_hosts import FILES
    rows = []
    for host, relative in PATHS.items():
        path = root / relative
        if not path.exists() and not (root / FILES[host]).exists():
            continue
        try:
            ok = path.read_text() == render(host)
            detail = "managed plugin matches; restart the agent to load it"
            if host == "hermes":
                from ctx.mcp_hosts import _hermes
                profile = Path(_hermes("path").stdout.strip())
                active = profile.parent / "plugins" / "straitjacket" / "__init__.py"
                enabled = _hermes("get", "plugins.enabled", "--json")
                disabled = _hermes("get", "plugins.disabled", "--json")
                enabled_names = json.loads(enabled.stdout) if enabled.returncode == 0 else []
                disabled_names = json.loads(disabled.stdout) if disabled.returncode == 0 else []
                ok = (ok and active.read_text() == render(host)
                      and "straitjacket" in (enabled_names or [])
                      and "straitjacket" not in (disabled_names or []))
                detail = "active profile plugin and enable list checked; restart Hermes"
            rows.append((f"{host} native hooks", bool(ok), detail))
        except (OSError, ValueError):
            rows.append((f"{host} native hooks", False, "plugin missing, modified, or not enabled; rerun ctx setup"))
    return rows


def worker_files(host: str, root: Path, source: Path | None = None):
    """Temporary worker wiring, removed before an isolated patch is captured.

    All writes are restored byte-for-byte. Never replace an existing worker
    config: that agent's project settings remain authoritative.
    """
    import contextlib
    from ctx.mcp_hosts import IntegrationError

    @contextlib.contextmanager
    def prepared():
        created = []
        directories = []
        def write_missing(path, data):
            if path.exists():
                return
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise IntegrationError("Refusing a symlink in ACP worker configuration")
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            path.parent.mkdir(parents=True, exist_ok=True)
            directories.extend(reversed(missing))
            path.write_bytes(data)
            created.append(path)
        try:
            if source and source.resolve() != root.resolve():
                host_configs = {"claude": (".claude/settings.json",),
                                "codex": (".codex/hooks.json", ".codex/config.toml")}
                for relative in ("ctx.toml", ".ctxignore", *host_configs.get(host, ())):
                    original = source / relative
                    if original.is_file():
                        write_missing(root / relative, original.read_bytes())
            if host in ("omp", "opencode", "dsh"):
                write_missing(root / PATHS[host], render(host).encode())
            if host == "hermes":
                # The enabled active-profile plugin is loaded by Hermes; a
                # worktree without a policy still needs the default marker.
                write_missing(root / "ctx.toml", b"version = 1\n")
            yield
        finally:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            for directory in reversed(directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
    return prepared()
