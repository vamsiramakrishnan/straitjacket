"""M-K2 file-set algebra (docs/SUBSTRATE.md §4): the ``file_select``
operator class — a bounded, receipted answer to *"which files may the
next operation touch?"* Logical surfaces: the ``corpus`` q source stage
and the ``repo.files`` plan op; this module is their shared physical
layer.

Engine ladder (git ls-files → fd → os.walk; the ripgrep pattern —
opportunistic binary, labeled, deterministic):

* **git rung** — a git workspace with gitignore semantics lists via
  ``Workspace.list_files`` (git ls-files inside).
* **fd rung** — otherwise ``fd``/``fdfind`` on PATH accelerates the walk.
  fd runs with ``--no-ignore`` so OUR ignore filter stays the single
  source of truth; every engine ends in the same ``is_ignored`` filter
  plus terminal sort, which is what makes the listings byte-identical by
  construction rather than by luck.
* **walk rung** — stdlib ``os.walk`` via ``Workspace.list_files``.

Kill-switch: ``CTX_FILES_ENGINE=python`` disables the fd binary;
``CTX_FILES_ENGINE=fd`` forces the fd rung (parity tests) with a labeled
fallback when the binary is absent. Absence never errors.

``changed=True`` binds to the generation snapshot (git porcelain — the
``changed(file, generation)`` fact plane), never to mtime: wall-clock
recency is volatile, machine-local, and unreplayable (SUBSTRATE §2.4).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from ctx.sessiondir import LEDGER_DIR_NAME
from ctx.workspace import Workspace

_FD_TIMEOUT = 30


def _fd_binary() -> str | None:
    """The fd executable name, or None (absent / killed by env)."""
    if os.environ.get("CTX_FILES_ENGINE") == "python":
        return None
    for name in ("fd", "fdfind"):  # Debian ships the binary as fdfind
        if shutil.which(name):
            return name
    return None


def _fd_walk(ws: Workspace) -> list[str] | None:
    """Raw repo-relative listing via fd, or None to fall back.

    ``--no-ignore`` is deliberate: ignore semantics live in ONE place
    (``ws.is_ignored``, applied by the caller), so fd is purely a faster
    walk — parity with ``Workspace._walk`` is by construction."""
    exe = _fd_binary()
    if exe is None:
        return None
    if ws.config.workspace.follow_symlinks:
        return None  # fd -t f skips symlinks; keep parity with the walk rung
    argv = [
        exe, "--no-ignore", "--hidden", "--type", "f",
        "--exclude", ".git", "--color", "never", "-0",
    ]
    try:
        proc = subprocess.run(
            argv, cwd=str(ws.root), capture_output=True, timeout=_FD_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    rels: list[str] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "replace").replace("\\", "/")
        rels.append(rel.removeprefix("./").rstrip("/"))
    return rels


def enumerate_files(ws: Workspace) -> tuple[list[str], str]:
    """Deterministically sorted repo-relative listing plus the engine
    label that produced it (``git`` | ``fd`` | ``walk``)."""
    forced = os.environ.get("CTX_FILES_ENGINE", "auto")
    git_rung = ws.git is not None and ws.config.workspace.respect_gitignore
    if git_rung and forced != "fd":
        return ws.list_files(), "git"
    if forced != "python":
        rels = _fd_walk(ws)
        if rels is not None:
            return sorted({r for r in rels if r and not ws.is_ignored(r)}), "fd"
    # forced == "fd" with no binary degrades here, labeled — never errors.
    return ws.list_files(), ("git" if git_rung else "walk")


def _glob_match(rel: str, glob: str) -> bool:
    from ctx._retrieval.targets import _glob_match as gm  # one glob semantics

    return gm(rel, glob)


def select(
    ws: Workspace,
    *,
    exts: tuple[str, ...] | list[str] = (),
    globs: tuple[str, ...] | list[str] = (),
    excludes: tuple[str, ...] | list[str] = (),
    changed: bool = False,
    max_files: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """The file-set selection. Returns ``(rows, coverage, omitted)`` where
    rows are ``{"file", "size"}`` (path-sorted), coverage is the receipt
    (``considered``/``selected``/``engine`` [+ ``generation``]) and
    ``omitted`` counts rows dropped by ``max_files`` (declared, never
    silent). Multiple globs are OR; multiple excludes each subtract."""
    rels, engine = enumerate_files(ws)
    coverage: dict[str, Any] = {"engine": engine, "considered": len(rels)}
    rels = [r for r in rels if r.split("/")[0] != LEDGER_DIR_NAME]

    if exts:
        want = {str(e).lower().lstrip(".") for e in exts if str(e).strip()}
        rels = [
            r for r in rels
            if "." in r.rsplit("/", 1)[-1] and r.rsplit(".", 1)[-1].lower() in want
        ]
    if globs:
        rels = [r for r in rels if any(_glob_match(r, g) for g in globs)]
    for g in excludes:
        rels = [r for r in rels if not _glob_match(r, g)]

    if changed:
        # Generation facts, never mtime (SUBSTRATE §2.4): the same porcelain
        # snapshot that generation hashing and the changed(file, gen) fact
        # plane are built on — content-confirmed and replayable.
        gen = None
        chg: set[str] = set()
        try:
            from ctx import facts

            chg = set(facts.changed_files_snapshot(ws))
            gen = facts.current_generation(ws)
        except Exception:
            pass
        rels = [r for r in rels if r in chg]
        coverage["generation"] = gen

    selected = len(rels)
    coverage["selected"] = selected
    omitted = 0
    if max_files is not None and max_files >= 0 and selected > max_files:
        omitted = selected - max_files
        rels = rels[:max_files]

    rows: list[dict[str, Any]] = []
    for r in rels:
        try:
            size = int((ws.root / r).stat().st_size)
        except OSError:
            size = 0
        rows.append({"file": r, "size": size})
    return rows, coverage, omitted
