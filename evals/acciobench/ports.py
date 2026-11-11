"""Cached port discovery and single-pass pre-compiled surgical port rewriting."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

_WORKSPACE_SCAN_SUFFIXES = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".sh", ".py", ".env"})

_HOST_PORT_SCAN_RE = re.compile(r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0):(\d{4,5})\b")
_SERVICE_PORT_SCAN_RE = re.compile(
    r"(?:\b[A-Z0-9_]*PORT\s*=\s*[\"']?(?:\$\{[A-Z0-9_:-]*:-)?|:-|--port[\s=]+[\"']?|default=)(\d{4,5})\b"
)

_BASE_PREFIX_SRC = (
    r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0):"
    r"|(?i:[\"']?port[\"']?\s*:\s*|--port(?:\s+|=)[\"']?|\b[A-Z0-9_]*PORT\s*=\s*[\"']?)"
)
_SERVICE_PREFIX_SRC = (
    _BASE_PREFIX_SRC
    + r"|default\s*=\s*"
    + r"|\bconst\s+PORT\s*=\s*"
    + r"|\bPORT\s*=\s*"
    + r"|\bstart_one\s+\S+\s+"
)


class CompiledPortMap:
    """Single-pass compiled regex rewriter for per-slot localhost port remapping."""

    __slots__ = ("_mapping", "_str_map", "_has_changes", "_base_re", "_service_re")

    def __init__(self, port_map: dict[int, int]) -> None:
        self._mapping: dict[int, int] = {int(k): int(v) for k, v in port_map.items()}
        self._str_map: dict[str, str] = {
            str(old_p): str(new_p) for old_p, new_p in self._mapping.items()
        }
        changed_ports = [str(old_p) for old_p, new_p in self._mapping.items() if old_p != new_p]
        self._has_changes = bool(changed_ports)
        if self._has_changes:
            # Sort longest-first so 5-digit ports precede 4-digit prefixes cleanly.
            port_alt = "|".join(sorted(changed_ports, key=len, reverse=True))
            self._base_re: re.Pattern[str] | None = re.compile(
                rf"(?P<p1>{_BASE_PREFIX_SRC})(?P<port1>{port_alt})\b"
                rf"|(?P<p2>:-)(?P<port2>{port_alt})(?P<s2>\}})"
            )
            self._service_re: re.Pattern[str] | None = re.compile(
                rf"(?P<p1>{_SERVICE_PREFIX_SRC})(?P<port1>{port_alt})\b"
                rf"|(?P<p2>:-)(?P<port2>{port_alt})(?P<s2>\}})"
            )
        else:
            self._base_re = None
            self._service_re = None

    @property
    def mapping(self) -> dict[int, int]:
        return dict(self._mapping)

    def __bool__(self) -> bool:
        return bool(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def __iter__(self) -> Iterator[int]:
        return iter(self._mapping)

    def __contains__(self, port: object) -> bool:
        return port in self._mapping

    def __getitem__(self, port: int) -> int:
        return self._mapping[port]

    def get(self, port: int, default: int | None = None) -> int | None:
        return self._mapping.get(port, default)

    def items(self):
        return self._mapping.items()

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CompiledPortMap):
            return self._mapping == other._mapping
        if isinstance(other, dict):
            return self._mapping == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"CompiledPortMap({self._mapping!r})"

    def _replace_match(self, match: re.Match[str]) -> str:
        port1 = match.group("port1")
        if port1 is not None:
            return f"{match.group('p1')}{self._str_map[port1]}"
        port2 = match.group("port2")
        return f"{match.group('p2')}{self._str_map[port2]}{match.group('s2')}"

    def rewrite(self, text: str, *, is_service_script: bool = False) -> str:
        if not text or not self._has_changes:
            return text
        pattern = self._service_re if is_service_script else self._base_re
        assert pattern is not None
        return pattern.sub(self._replace_match, text)


@lru_cache(maxsize=256)
def _compile_cached_dict(items: tuple[tuple[int, int], ...]) -> CompiledPortMap:
    return CompiledPortMap(dict(items))


@lru_cache(maxsize=512)
def _discover_task_ports_cached(
    case_dir_str: str,
    host_published_port: int | None,
    mock_ports: tuple[int, ...],
    has_runtime_mocks: bool,
) -> tuple[int, ...]:
    case_dir = Path(case_dir_str)
    has_services = has_runtime_mocks or (case_dir / "services").exists()
    if not has_services and host_published_port is None:
        return ()

    ports: set[int] = set(mock_ports)
    if host_published_port:
        ports.add(host_published_port)

    scan_paths: list[Path] = [case_dir / "task.md"]
    for sub in ("services", "grader"):
        sd = case_dir / sub
        if sd.exists():
            scan_paths.extend(
                p for p in sd.rglob("*") if p.is_file() and "node_modules" not in p.parts
            )
    ws = case_dir / "workspace"
    if ws.exists():
        scan_paths.extend(
            p
            for p in ws.rglob("*")
            if p.is_file() and p.suffix.lower() in _WORKSPACE_SCAN_SUFFIXES
        )

    for p in scan_paths:
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pm in _HOST_PORT_SCAN_RE.findall(txt):
            pv = int(pm)
            if 1024 <= pv <= 65535:
                ports.add(pv)
        if p.parent.name in ("services", "logistics_tracker", "grader"):
            for pm in _SERVICE_PORT_SCAN_RE.findall(txt):
                pv = int(pm)
                if 3000 <= pv <= 9999:
                    ports.add(pv)

    return tuple(sorted(ports))


def discover_task_ports(spec: Any) -> list[int]:
    """Discover all localhost service ports used by a task (cached in memory by case_dir)."""
    case_dir_str = str(Path(spec.case_dir).resolve())
    hp_raw = getattr(spec, "host_published_port", None)
    host_published_port = int(hp_raw) if hp_raw is not None else None
    runtime_mocks = getattr(spec, "runtime_mocks", None) or ()
    mock_ports = tuple(
        sorted({int(m.port) for m in runtime_mocks if getattr(m, "port", None)})
    )
    return list(
        _discover_task_ports_cached(
            case_dir_str,
            host_published_port,
            mock_ports,
            bool(runtime_mocks),
        )
    )


discover_task_ports.cache_info = _discover_task_ports_cached.cache_info  # type: ignore[attr-defined]
discover_task_ports.cache_clear = _discover_task_ports_cached.cache_clear  # type: ignore[attr-defined]


def rewrite_ports_surgical(
    text: str,
    port_map: dict[int, int] | CompiledPortMap,
    *,
    is_service_script: bool = False,
) -> str:
    """Surgically rewrite localhost ports in URLs, CLI flags, env vars, and service configs."""
    if not port_map or not text:
        return text
    if isinstance(port_map, CompiledPortMap):
        return port_map.rewrite(text, is_service_script=is_service_script)
    compiled = _compile_cached_dict(tuple(sorted((int(k), int(v)) for k, v in port_map.items())))
    return compiled.rewrite(text, is_service_script=is_service_script)
