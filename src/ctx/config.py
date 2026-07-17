"""Repository policy: committed ``ctx.toml`` plus hard defaults from SPEC §13."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "ctx.toml"
IGNORE_FILENAME = ".ctxignore"

# Secret-bearing paths excluded from automatic capture regardless of config
# (SPEC §5.5). ``.ctxignore`` adds to these; nothing can remove them silently.
BUILTIN_DENY_GLOBS = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/secrets/**",
    "**/credentials/**",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.aws/**",
    "**/.config/gcloud/**",
    "**/.ssh/**",
)


@dataclass(frozen=True)
class Budgets:
    digest_tokens: int = 480
    result_tokens: int = 1200
    turn_retrieval_tokens: int = 2800
    max_inline_bytes: int = 16384
    max_inline_lines: int = 240
    max_matches: int = 80


@dataclass(frozen=True)
class Guard:
    mode: str = "guarded"  # advisory | guarded | strict
    unknown_command: str = "force_ask"  # allow | deny | ask | force_ask
    internal_error: str = "allow"  # availability-safe default (SPEC §10.2)


@dataclass(frozen=True)
class StorePolicy:
    backend: str = "user-state"  # user-state | local (advisory only)
    retention_days: int = 30


@dataclass(frozen=True)
class WorkspacePolicy:
    allow_outside_root: bool = False
    follow_symlinks: bool = False
    nested_repos: str = "separate"
    respect_gitignore: bool = True


def _all_redaction_patterns() -> tuple[str, ...]:
    from ctx.textutil import REDACTION_PATTERNS

    return tuple(REDACTION_PATTERNS)


@dataclass(frozen=True)
class Redaction:
    enabled: bool = True
    # Default: every vendored pattern. A committed ctx.toml may narrow this.
    patterns: tuple[str, ...] = field(default_factory=_all_redaction_patterns)


@dataclass(frozen=True)
class Config:
    version: int = 1
    repo_key: str | None = None
    workspace: WorkspacePolicy = field(default_factory=WorkspacePolicy)
    budgets: Budgets = field(default_factory=Budgets)
    guard: Guard = field(default_factory=Guard)
    store: StorePolicy = field(default_factory=StorePolicy)
    redaction: Redaction = field(default_factory=Redaction)
    scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    deny_globs: tuple[str, ...] = BUILTIN_DENY_GLOBS


def _pick(data: dict[str, Any], cls: type, **overrides: Any) -> Any:
    names = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in names}
    kwargs.update(overrides)
    return cls(**kwargs)


def load_config(workspace_root: Path | None) -> Config:
    """Load ``ctx.toml`` from the workspace root; malformed files degrade to
    defaults rather than blocking the harness (fail-open, SPEC §15)."""
    if workspace_root is None:
        return Config()
    path = workspace_root / CONFIG_FILENAME
    if not path.is_file():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return Config()

    scopes: dict[str, tuple[str, ...]] = {}
    for name, body in (raw.get("scopes") or {}).items():
        roots = body.get("roots") if isinstance(body, dict) else None
        if isinstance(roots, list):
            scopes[str(name)] = tuple(str(r) for r in roots)

    budgets = _pick(raw.get("budgets") or {}, Budgets)
    guard = _pick(raw.get("guard") or {}, Guard)
    store = _pick(raw.get("store") or {}, StorePolicy)
    ws = _pick(raw.get("workspace") or {}, WorkspacePolicy)

    red_raw = raw.get("redaction") or {}
    redaction = Redaction(
        enabled=bool(red_raw.get("enabled", True)),
        patterns=tuple(red_raw.get("patterns", Redaction().patterns)),
    )

    return Config(
        version=int(raw.get("version", 1)),
        repo_key=raw.get("repo_key"),
        workspace=ws,
        budgets=budgets,
        guard=guard,
        store=store,
        redaction=redaction,
        scopes=scopes,
    )


def load_ctxignore(workspace_root: Path | None) -> tuple[str, ...]:
    """Globs from ``.ctxignore`` merged with the built-in secret deny list."""
    patterns: list[str] = list(BUILTIN_DENY_GLOBS)
    if workspace_root is not None:
        path = workspace_root / IGNORE_FILENAME
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
            except (OSError, UnicodeDecodeError):
                pass
    return tuple(dict.fromkeys(patterns))
