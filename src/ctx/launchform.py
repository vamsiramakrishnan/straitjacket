"""Where installed ctx commands live, which of them still launch ctx in the
unsafe ``python -m ctx`` form, and how each one gets fixed.

``python -m ctx`` imports ``ctx`` from the host's working directory (the
user's workspace), so a repository could replace the guard. Releases before
the fix wrote that form whenever ``ctx`` was not on PATH. This module:

* lists every file a ctx installer writes a command into, for every host,
  in workspace and user scope, plus the host's own settings files where a
  hook or MCP command can also live (:func:`locations`) — derived from the
  installers' own tables, so a new host cannot be forgotten;
* finds unsafe commands in them (:func:`scan`), conservatively;
* rewrites ONLY commands ctx itself wrote, recognised by their exact shape,
  in the files ctx manages (:func:`upgrade`), even where the rest of the
  file is something setup refuses to reconcile. Everything else gets the
  exact replacement text from ``ctx doctor``, never a promise that setup
  will do it.

Files whose ctx entries are tracked by the host-config ownership ledger
(Codex and OpenCode installed by current setup) are left to that ledger,
which rewrites its owned values itself; editing them here would break the
ledger's hashes.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ctx.selfcmd import python_m_ctx

_SERVERS = ("ctx-harness", "ctx-surface-gateway")
_HOOK_FLAVORS = ("claude-code", "codex", "antigravity", "hermes", "omp", "opencode", "dsh")


@dataclass(frozen=True)
class Location:
    host: str
    path: Path
    scope: str          # workspace | user
    managed: bool       # a ctx installer writes commands here
    fix: str = ""       # the command that rewrites ctx-owned entries here ("" = none)
    ledger: bool = False  # ctx entries tracked by the host-config ledger
    root: Path | None = None  # boundary for the symlink check (workspace root)


@dataclass
class Finding:
    location: Location
    where: str
    old: list[str]
    new: list[str]
    owned: bool          # exactly a shape ctx itself writes
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fixed_by_setup(self) -> bool:
        loc = self.location
        return (self.owned and loc.managed and bool(loc.fix)
                and write_refusal(loc.path, loc.root) is None)

    def message(self, root: Path) -> str:
        rel = _display(self.location.path, root)
        if self.fixed_by_setup:
            return f"{rel} ({self.where}): `{self.location.fix}` rewrites it to the -P form"
        why = ("ctx does not manage this entry" if not (self.owned and self.location.managed)
               else f"setup will not rewrite it ({write_refusal(self.location.path, self.location.root)})")
        return f"{rel} ({self.where}): {why}; {self.replacement()}"

    def replacement(self) -> str:
        """The exact edit, in the SHAPE of the entry (command + args, a
        command array, or one command string)."""
        kind = self.extra.get("kind")
        if kind == "server" and isinstance(self.extra.get("value"), str):
            return (f"set \"command\": {json.dumps(self.new[0])}, \"args\": "
                    f"{json.dumps(self.new[1:])}")
        if kind == "server":
            return f"set \"command\": {json.dumps(self.new)}"
        return f"replace `{shlex.join(self.old)}` with `{shlex.join(self.new)}`"


def _display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        home = os.environ.get("HOME") or ""
        s = str(path)
        return "~" + s[len(home):] if home and s.startswith(home + os.sep) else s


# ------------------------------------------------------------------ registry
def locations(root: Path | str) -> list[Location]:
    """Every file to read, derived from the installers' own tables."""
    from ctx import mcp_hosts, native_hooks, surface
    from ctx.host_config import owned_hosts
    from ctx.installer import (PLUGIN_DIRNAME, _GATEWAY_FILES, _PLUGIN_FILES,
                               antigravity_settings_path)

    root = Path(root)
    ledger = set(owned_hosts(root))
    out: list[Location] = [
        # Claude Code: ctx writes .claude/settings.json; the rest are the
        # host's own files where a hook or MCP command can also live.
        Location("claude", root / ".claude" / "settings.json", "workspace", True,
                 "ctx setup --host claude"),
        Location("claude", root / ".claude" / "settings.local.json", "workspace", False),
        Location("claude", root / ".mcp.json", "workspace", False),
        Location("claude", surface.claude_config_dir() / "settings.json", "user", False),
        Location("claude", surface.claude_user_json(), "user", False),
        Location("codex", Path(os.environ.get("HOME") or Path.home()) / ".codex" / "config.toml",
                 "user", False),
        Location("antigravity", antigravity_settings_path(), "user", True,
                 "ctx setup --host antigravity"),
    ]
    from ctx.host_config import HOSTS as LEDGER_HOSTS, opencode_path

    # Instruction blocks name the ctx command the model should run.
    out.append(Location("claude", root / "CLAUDE.md", "workspace", True, "ctx setup --host claude"))
    out.append(Location("codex", root / "AGENTS.md", "workspace", True, "ctx setup --host codex",
                        ledger="codex" in ledger))
    codex_files = (".codex/config.toml", ".codex/hooks.json")
    for rel in codex_files:
        out.append(Location("codex", root / rel, "workspace", True, "ctx setup --host codex",
                            ledger="codex" in ledger))
    for name in _PLUGIN_FILES:
        out.append(Location("antigravity", root / ".agents" / "plugins" / PLUGIN_DIRNAME / name,
                            "workspace", True, "ctx setup --host antigravity"))
    for host, rel in mcp_hosts.FILES.items():
        if host == "opencode":
            rel = str(opencode_path(root))
        out.append(Location(host, root / rel, "workspace", True, f"ctx setup --host {host}",
                            ledger=host in LEDGER_HOSTS and host in ledger))
    for host, rel in native_hooks.PATHS.items():
        out.append(Location(host, root / rel, "workspace", True, f"ctx setup --host {host}",
                            ledger=host in LEDGER_HOSTS and host in ledger))
    profile_plugin = _hermes_profile_plugin()
    if profile_plugin is not None:
        out.append(Location("hermes", profile_plugin, "user", True, "ctx setup --host hermes"))
    for host, rel in _GATEWAY_FILES.items():
        out.append(Location(f"gateway-{host}", root / rel, "workspace", True,
                            f"ctx surface install-gateway --host {host} --apply"))
    seen: set[Path] = set()
    unique = []
    for loc in out:
        if loc.path not in seen:
            seen.add(loc.path)
            unique.append(loc)
    return [replace(l, root=root) if l.scope == "workspace" else l for l in unique]


def _hermes_profile_plugin() -> Path | None:
    """The active Hermes profile's straitjacket plugin, when Hermes is on
    PATH (its location is only known through the Hermes CLI)."""
    import shutil

    if not shutil.which("hermes"):
        return None
    try:
        from ctx.mcp_hosts import _hermes

        result = _hermes("path")
        profile = Path(result.stdout.strip())
        if result.returncode or not profile.is_absolute():
            return None
        return profile.parent / "plugins" / "straitjacket" / "__init__.py"
    except Exception:
        return None


# ------------------------------------------------------------------ scanning
_ARGV_LITERAL = re.compile(r"(?:const ctxArgv|CTX_ARGV) = (\[[^\n]*\])")


def _owned_command_string(tokens: list[str]) -> bool:
    if len(tokens) < 4 or tokens[1:3] != ["-m", "ctx"]:
        return False
    if not os.path.basename(tokens[0]).startswith("python"):
        return False
    rest = tokens[3:]
    return ((len(rest) == 3 and rest[0] == "hook" and rest[1] in _HOOK_FLAVORS)
            or (len(rest) == 2 and rest[0] == "statusline"))


def _owned_server_argv(argv: list[str], server: str) -> bool:
    return (server in _SERVERS and len(argv) >= 4 and argv[1:3] == ["-m", "ctx"]
            and os.path.basename(argv[0]).startswith("python")
            and (argv[3] == "mcp" or argv[3:5] == ["surface", "gateway"]))


def _safe(argv: list[str]) -> list[str]:
    """The replacement: ``-P`` right after the interpreter."""
    return [argv[0], "-P", *argv[1:]]


def _read(path: Path) -> str | None:
    from ctx.surface import _read_text

    return _read_text(path)


def scan_location(loc: Location) -> list[Finding]:
    text = _read(loc.path)
    if text is None:
        return []
    found: list[Finding] = []
    if loc.path.suffix == ".md":
        from ctx.installer import _CODEX_MARK_END, _CODEX_MARK_START

        a, b = text.find(_CODEX_MARK_START), text.find(_CODEX_MARK_END)
        for m in re.finditer(r"`([^`\n]+)`", text):
            try:
                argv = shlex.split(m.group(1))
            except ValueError:
                continue
            hit = python_m_ctx(argv)
            if hit and not hit[1]:
                owned = (0 <= a < m.start() < b and argv[1:] == ["-m", "ctx"]
                         and os.path.basename(argv[0]).startswith("python"))
                found.append(Finding(loc, "instruction block", argv, _safe(argv), owned,
                                     {"literal": m.group(0)}))
        return found
    if loc.path.suffix in (".js", ".mjs", ".py"):
        for m in _ARGV_LITERAL.finditer(text):
            try:
                argv = [str(a) for a in json.loads(m.group(1))]
            except ValueError:
                continue
            hit = python_m_ctx(argv)
            if hit and not hit[1]:
                from ctx.native_hooks import MARKER

                owned = MARKER in text and argv[1:] == ["-m", "ctx"]
                found.append(Finding(loc, "bridge argv", argv, _safe(argv), owned,
                                     {"literal": m.group(1)}))
        return found
    try:
        if loc.path.suffix == ".toml":
            import tomllib

            doc = tomllib.loads(text)
        else:
            doc = json.loads(text)
    except Exception:
        # jsonc / near-JSON: still report command strings, never rewrite.
        for m in re.finditer(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
            try:
                cmd = json.loads(f'"{m.group(1)}"')
                argv = shlex.split(cmd)
            except ValueError:
                continue
            hit = python_m_ctx(argv)
            if hit and not hit[1]:
                found.append(Finding(loc, "command", argv, _safe_or_same(argv), False))
        return found

    def walk(value: Any, where: str, server: str = "") -> None:
        if isinstance(value, dict):
            cmd = value.get("command")
            args = value.get("args")
            argv: list[str] | None = None
            kind = ""
            if isinstance(cmd, str) and isinstance(args, list):
                argv, kind = [cmd, *[str(a) for a in args]], "server"
            elif isinstance(cmd, list):
                argv, kind = [str(a) for a in cmd], "server"
            elif isinstance(cmd, str):
                try:
                    argv, kind = shlex.split(cmd), "string"
                except ValueError:
                    argv = None
            if argv:
                hit = python_m_ctx(argv)
                if hit and not hit[1]:
                    owned = (_owned_command_string(argv) if kind == "string"
                             else _owned_server_argv(argv, str(value.get("serverName") or server)))
                    found.append(Finding(loc, where or "command", argv, _safe_or_same(argv),
                                         owned, {"kind": kind, "value": cmd, "args": args}))
            named = next((str(value.get(k)) for k in ("serverName", "id", "name")
                          if str(value.get(k)) in _SERVERS), "")
            for k, v in value.items():
                walk(v, f"{where}.{k}" if where else str(k),
                     server=str(k) if str(k) in _SERVERS else (named or server))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{where}[{i}]", server)

    walk(doc, "")
    return found


def _safe_or_same(argv: list[str]) -> list[str]:
    """Replacement text for a report: -P inserted right after the
    interpreter (correct for any python launch)."""
    return [argv[0], "-P", *argv[1:]]


def scan(root: Path | str) -> list[Finding]:
    out: list[Finding] = []
    for loc in locations(root):
        try:
            out += scan_location(loc)
        except Exception:
            continue
    return out


# ------------------------------------------------------------------ upgrading
def _rewrite(loc: Location, findings: list[Finding]) -> bool:
    """Rewrite the owned findings of one file in place. Minimal textual
    edits, so formatting and anything else in the file are untouched."""
    text = _read(loc.path)
    if text is None:
        return False
    new = text
    for f in findings:
        if loc.path.suffix in (".js", ".mjs", ".py"):
            new = new.replace(f.extra["literal"], json.dumps(f.new), 1)
            continue
        if loc.path.suffix == ".md":
            new = new.replace(f.extra["literal"], "`" + shlex.join(f.new) + "`")
            continue
        if f.extra.get("kind") == "string":
            old_q = json.dumps(f.extra["value"])
            new_q = json.dumps(shlex.join(f.new))
            new = new.replace(old_q, new_q)
            continue
        # A server entry: "-m" as the first arg (string command + args) or
        # the interpreter followed by "-m" (command array).
        q_py = re.escape(json.dumps(f.old[0]))
        if isinstance(f.extra.get("value"), list):
            pat = re.compile(rf'({q_py}\s*,\s*)"-m"(\s*,\s*)"ctx"')
            new, n = pat.subn(r'\1"-P"\2"-m"\2"ctx"', new, count=1)
        else:
            pat = re.compile(r'((?:"args"|\bargs)\s*[:=]\s*\[\s*)"-m"(\s*,\s*)"ctx"')
            new, n = pat.subn(r'\1"-P"\2"-m"\2"ctx"', new, count=1)
    if new == text:
        return False
    replace_file_safely(loc.path, new, root=loc.root)
    return True


class UnsafeWrite(Exception):
    """A rewrite was refused; the message says why (shown to the user)."""


def write_refusal(path: Path, root: Path | None) -> str | None:
    """Why ``path`` must not be rewritten in place, or None.

    Refused: the target is a symlink; a directory between ``root`` and the
    target is a symlink leading outside ``root``; the user made the file
    read-only."""
    import stat as _stat

    try:
        if path.is_symlink():
            return "it is a symlink"
        why = _dir_refusal(path.parent, root)
        if why:
            return why
        st = path.stat()
        if not st.st_mode & _stat.S_IWUSR:
            return "it is read-only"
    except OSError as exc:
        return f"it cannot be inspected ({type(exc).__name__})"
    return None


def _dir_refusal(directory: Path, root: Path | None) -> str | None:
    """A directory between ``root`` and ``directory`` that is a symlink
    leading outside ``root``, as a reason; else None."""
    if root is None:
        return None
    root_real = Path(os.path.realpath(root))
    try:
        rel = directory.relative_to(root)
    except ValueError:
        return None
    cur = Path(root)
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink() and not _within(Path(os.path.realpath(cur)), root_real):
            return f"{cur.name}/ is a symlink leading outside the workspace"
    return None


def write_safely(path: Path, text: str, *, root: Path | None, mode: int = 0o644) -> None:
    """Create or replace ``path`` without ever writing through a link or a
    directory symlinked outside ``root``. Raises :class:`UnsafeWrite`."""
    if path.exists() or path.is_symlink():
        replace_file_safely(path, text, root=root)
        return
    why = _dir_refusal(path.parent, root)
    if why:
        raise UnsafeWrite(why)
    path.parent.mkdir(parents=True, exist_ok=True)
    why = _dir_refusal(path.parent, root)   # re-check after creating
    if why:
        raise UnsafeWrite(why)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def replace_file_safely(path: Path, text: str, *, root: Path | None = None) -> None:
    """Replace ``path``'s contents atomically without ever writing through a
    link: the new bytes go to a fresh file created exclusively (O_EXCL, a
    random name) in the same directory with the target's mode, which is
    then renamed over the target. Raises :class:`UnsafeWrite` when
    :func:`write_refusal` objects."""
    import tempfile

    why = write_refusal(path, root)
    if why:
        raise UnsafeWrite(why)
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".ctx-upgrade", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upgrade(root: Path | str, hosts: list[str] | tuple[str, ...]) -> list[str]:
    """Rewrite every ctx-owned unsafe command in the files these hosts'
    setup manages (ledger-tracked files excepted). One line when anything
    changed. Never raises."""
    try:
        by_loc: dict[Path, tuple[Location, list[Finding]]] = {}
        for f in scan(root):
            loc = f.location
            if not (f.owned and loc.managed and loc.host in hosts and not loc.ledger):
                continue
            by_loc.setdefault(loc.path, (loc, []))[1].append(f)
        lines: list[str] = []
        for loc, fs in by_loc.values():
            rel = _display(loc.path, Path(root))
            try:
                if _rewrite(loc, fs):
                    lines.append(f"updated {len(fs)} ctx command(s) in {rel} from the unsafe "
                                 "`python -m ctx` form to `python -P -m ctx` (the old form could "
                                 "run a `ctx` package from the workspace)")
            except UnsafeWrite as exc:
                lines.append(f"left {rel} unchanged ({exc}): it still has {len(fs)} unsafe ctx "
                             "command(s); `ctx doctor` prints the exact replacement")
    except Exception as exc:  # noqa: BLE001 — never break setup
        return [f"could not upgrade unsafe ctx commands ({type(exc).__name__}); `ctx doctor` lists them"]
    return lines


__all__ = ["Location", "Finding", "locations", "scan", "scan_location", "upgrade"]
