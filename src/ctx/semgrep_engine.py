"""Semgrep engine tier (docs/EVIDENCE-PLANS.md P3).

Semgrep answers constrained semantic questions ast-grep cannot: constant
propagation, import-resolved matching, and taint (source → propagator →
sanitizer → sink). It is another *fact producer* for the shared evidence
graph — never a replacement for the fact store.

**Hermetic by construction** (binding): local rule files only (confined to
the workspace), ``--metrics=off``, no version check, no registry fetch —
a network-fetching analyzer inside a deterministic evidence plane is a
non-starter. Absence is a declared skip, never an error.

Determinism: findings are sorted ``(path, line, rule)``, paths repo-relative
POSIX, messages capped; the semgrep version is disclosed in node meta and
participates in node cache keys.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from typing import Any

from ctx.textutil import EVIDENCE_LINE_CHARS
from ctx.workspace import Workspace

_PROBE_TIMEOUT = 15.0
_RUN_TIMEOUT = 180.0


class EngineMissing(Exception):
    pass


class SemgrepError(Exception):
    pass


@lru_cache(maxsize=1)
def binary() -> tuple[str, str] | None:
    path = shutil.which("semgrep")
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, timeout=_PROBE_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    version = (out.stdout or out.stderr).decode("utf-8", "replace").strip().splitlines()
    return path, (version[-1].split()[-1] if version else "?")


def available() -> bool:
    return binary() is not None


def engine_id() -> str:
    b = binary()
    return f"semgrep {b[1]}" if b else "semgrep (absent)"


def _trace_frames(extra: dict[str, Any]) -> list[str]:
    """Flatten a dataflow trace into ``file:line`` frames, order preserved.
    Schema-tolerant: collects every {path, start.line} pair the trace
    carries, whatever its nesting; absent trace → []."""
    trace = extra.get("dataflow_trace")
    if trace is None:
        return []
    out: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            path = node.get("path")
            start = node.get("start")
            if isinstance(path, str) and isinstance(start, dict) and "line" in start:
                out.append(f"{path.replace(chr(92), '/')}:{start['line']}")
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(trace)
    return list(dict.fromkeys(out))


def scan(
    ws: Workspace,
    rules_rel: str,
    *,
    paths: list[str] | None = None,
    cap: int = 200,
    timeout: float = _RUN_TIMEOUT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one hermetic scan with a workspace-local rules file. Returns
    (rows, meta); raises :class:`EngineMissing` when semgrep is absent
    (the op layer maps that to a declared skip by default)."""
    b = binary()
    if b is None:
        raise EngineMissing("semgrep is not installed (pip extra: ctx-harness[sem])")
    rules_path = ws.confine(rules_rel, must_exist=True)

    scan_paths = []
    for p in paths or ["."]:
        confined = ws.confine(p, must_exist=True)
        scan_paths.append(str(ws.relativize(confined) or "."))

    argv = [
        b[0],
        "scan",
        "--json",
        "--quiet",
        "--metrics=off",
        "--disable-version-check",
        "--config",
        str(rules_path),
        *scan_paths,
    ]
    try:
        out = subprocess.run(
            argv, cwd=str(ws.root), capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise SemgrepError(f"semgrep timed out after {timeout:.0f}s") from e
    except (OSError, subprocess.SubprocessError) as e:
        raise SemgrepError(f"semgrep failed to run: {e}") from e
    # Exit 0 = clean, 1 = findings; anything else is a real failure.
    if out.returncode not in (0, 1):
        tail = out.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SemgrepError("semgrep failed: " + (tail[-1] if tail else f"exit {out.returncode}"))
    try:
        doc = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as e:
        raise SemgrepError(f"semgrep emitted unparseable JSON: {e}") from e

    rows: list[dict[str, Any]] = []
    for res in doc.get("results") or []:
        if not isinstance(res, dict):
            continue
        path = str(res.get("path") or "").replace("\\", "/")
        start = res.get("start") or {}
        extra = res.get("extra") or {}
        row: dict[str, Any] = {
            "rule": str(res.get("check_id") or ""),
            "file": path,
            "line": int(start.get("line", 0) or 0),
            "message": str(extra.get("message") or "").strip()[:EVIDENCE_LINE_CHARS],
        }
        frames = _trace_frames(extra) if isinstance(extra, dict) else []
        if frames:
            row["trace"] = frames[:16]
        rows.append(row)
    rows.sort(key=lambda r: (r["file"], r["line"], r["rule"]))
    errors = doc.get("errors") or []
    meta: dict[str, Any] = {
        "engine": engine_id(),
        "precision": "semantic",
        "rules": str(rules_rel),
        "matched": len(rows),
    }
    if errors:
        meta["parser_warnings"] = len(errors)
    return rows[: max(1, cap)], meta


__all__ = ["EngineMissing", "SemgrepError", "available", "binary", "engine_id", "scan"]
