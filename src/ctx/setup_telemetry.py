"""Privacy-safe readiness receipt for the setup fast path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from ctx import __version__
from ctx.sessiondir import session_reads_path

SETUP_SCHEMA = "ctx.setup-receipt/v1"
_RECEIPT_NAME = "setup.json"
_MANAGED_FILES = (
    "ctx.toml",
    ".ctxignore",
    ".agents/plugins/ctx-harness/plugin.json",
    ".agents/plugins/ctx-harness/hooks.json",
    ".agents/plugins/ctx-harness/mcp_config.json",
    ".claude/settings.json",
    ".claude/agents/ctx-explorer.md",
    ".codex/config.toml",
    ".codex/hooks.json",
    "AGENTS.md",
    "CLAUDE.md",
)
_MANAGED_DIRS = (".agents/plugins/ctx-harness",)


def setup_fingerprint(workspace_root: Path) -> str:
    """Hash managed setup state without persisting config contents or paths."""
    digest = hashlib.sha256()
    digest.update(f"ctx-harness:{__version__}\0".encode())
    executable = shutil.which("ctx") or ""
    digest.update(f"ctx-on-path:{bool(executable)}\0".encode())
    if executable:
        # The path itself never leaves this one-way digest.  Including its
        # identity prevents `python -m ctx setup` from certifying an older or
        # replaced `ctx` executable that merely happens to share the PATH slot.
        resolved = Path(executable).resolve(strict=False)
        digest.update(os.fsencode(resolved) + b"\0")
        try:
            stat = resolved.stat()
        except OSError:
            digest.update(b"unstatable\0")
        else:
            digest.update(
                f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}\0".encode()
            )
    for relative in _MANAGED_FILES:
        path = workspace_root / relative
        digest.update(relative.encode() + b"\0")
        try:
            payload = path.read_bytes()
        except OSError:
            digest.update(b"missing\0")
        else:
            digest.update(hashlib.sha256(payload).digest())
    for relative in _MANAGED_DIRS:
        directory = workspace_root / relative
        digest.update(relative.encode() + b"\0")
        if not directory.is_dir():
            digest.update(b"missing\0")
            continue
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            child = path.relative_to(workspace_root).as_posix()
            digest.update(child.encode() + b"\0")
            try:
                payload = path.read_bytes()
            except OSError:
                digest.update(b"unreadable\0")
            else:
                digest.update(hashlib.sha256(payload).digest())

    # Antigravity's status line is global. Hash only that one setting, never the
    # surrounding user-owned configuration.
    home = os.environ.get("HOME") or str(Path.home())
    antigravity_settings = Path(home) / ".gemini" / "antigravity-cli" / "settings.json"
    digest.update(b"antigravity-statusLine\0")
    try:
        document = json.loads(antigravity_settings.read_text(encoding="utf-8"))
        status_line = document.get("statusLine") if isinstance(document, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        status_line = None
    digest.update(
        json.dumps(status_line, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return digest.hexdigest()


def load_setup_receipt(workspace_root: Path) -> dict[str, Any] | None:
    path = session_reads_path(workspace_root, _RECEIPT_NAME)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SETUP_SCHEMA else None


def setup_is_current(workspace_root: Path, hosts: Iterable[str]) -> bool:
    receipt = load_setup_receipt(workspace_root)
    receipt_parent = session_reads_path(workspace_root, _RECEIPT_NAME).parent
    return bool(
        receipt
        and os.access(receipt_parent, os.W_OK)
        and receipt.get("success") is True
        and receipt.get("ctx_version") == __version__
        and receipt.get("hosts") == sorted(set(hosts))
        and receipt.get("fingerprint") == setup_fingerprint(workspace_root)
    )


def record_setup(
    workspace_root: Path,
    hosts: Iterable[str],
    *,
    strategy: str,
    success: bool,
    checks_total: int,
    checks_passed: int,
    duration_ms: float,
) -> dict[str, Any]:
    """Atomically record structural setup facts; never config contents/paths."""
    receipt = {
        "schema": SETUP_SCHEMA,
        "recorded_at_unix": time.time(),
        "ctx_version": __version__,
        "hosts": sorted(set(hosts)),
        "strategy": strategy,
        "success": bool(success),
        "checks_total": int(checks_total),
        "checks_passed": int(checks_passed),
        "duration_ms": round(max(0.0, float(duration_ms)), 3),
        "prompts": 0,
        "manual_config_edits": 0,
        "fingerprint": setup_fingerprint(workspace_root),
    }
    path = session_reads_path(workspace_root, _RECEIPT_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    return receipt
