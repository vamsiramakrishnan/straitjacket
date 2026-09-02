"""Transactional Git worktrees for independently targeted mutation workers.

The orchestrator deliberately keeps this module small and stdlib-only.  A
worker runs against a detached worktree at the caller's current ``HEAD``.  Its
changes are captured as one binary patch, checked against declared targets,
and only then applied to the real workspace.  The temporary worktree is always
removed, including when the worker or patch capture fails.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class WorktreeIsolationError(RuntimeError):
    """An isolated checkout or its transactional patch could not be trusted."""


@dataclass(frozen=True)
class WorktreePatch:
    data: bytes
    changed_paths: tuple[str, ...]


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _git_error(proc: subprocess.CompletedProcess) -> str:
    return proc.stderr.decode("utf-8", "replace").strip()[:500] or "git command failed"


def normalize_targets(targets: tuple[str, ...]) -> tuple[str, ...]:
    """Return safe repository-relative targets, rejecting ambiguous paths."""
    normalized: list[str] = []
    for raw in targets:
        value = str(raw).strip().replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or path.parts[0] == ".git"
        ):
            raise WorktreeIsolationError(f"unsafe declared target: {raw!r}")
        clean = path.as_posix().removeprefix("./")
        if clean not in normalized:
            normalized.append(clean)
    return tuple(normalized)


def targets_overlap(groups: list[tuple[str, ...]]) -> bool:
    """Whether any two nodes declare the same path or ancestor/descendant paths."""
    normalized = [normalize_targets(group) for group in groups]
    for left_i, left in enumerate(normalized):
        for right in normalized[left_i + 1 :]:
            for a in left:
                for b in right:
                    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                        return True
    return False


def clean_git_root(root: Path) -> bool:
    """True only for an exact, clean Git worktree root."""
    root = root.resolve()
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return False
    resolved_top = Path(top.stdout.decode("utf-8", "replace").strip()).resolve()
    if resolved_top != root:
        return False
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        return False
    # The harness's own bookkeeping directory is never dirt. It is excluded by
    # name from retrieval, generation hashing and the census walk for the same
    # reason (ctx.sessiondir): the harness must not observe its own state. The
    # task ledger writes there BEFORE the first wave, and counting that as an
    # untracked change turned every isolated wave into the serial fallback.
    from ctx.sessiondir import LEDGER_DIR_NAME

    for line in status.stdout.decode("utf-8", "replace").splitlines():
        entry = line[3:].strip().strip('"')
        if entry == LEDGER_DIR_NAME or entry.startswith(LEDGER_DIR_NAME + "/"):
            continue
        if entry:
            return False
    return True


def _path_allowed(path: str, targets: tuple[str, ...]) -> bool:
    return any(path == target or path.startswith(target + "/") for target in targets)


class IsolatedWorktree:
    """A detached worktree that can emit one target-checked binary patch."""

    def __init__(self, root: Path, node_id: str, targets: tuple[str, ...]):
        self.root = root.resolve()
        self.node_id = node_id
        self.targets = normalize_targets(targets)
        self._temp_parent: Path | None = None
        self.path: Path | None = None

    def __enter__(self) -> "IsolatedWorktree":
        if not clean_git_root(self.root):
            raise WorktreeIsolationError("repository must be a clean, exact Git root")
        self._temp_parent = Path(tempfile.mkdtemp(prefix="ctx-worktree-"))
        self.path = self._temp_parent / "repo"
        added = _git(self.root, "worktree", "add", "--detach", os.fspath(self.path), "HEAD")
        if added.returncode != 0:
            self._cleanup()
            raise WorktreeIsolationError(f"could not create worktree: {_git_error(added)}")
        return self

    def reset(self) -> None:
        """Discard a failed attempt before an escalation retries in this checkout."""
        if self.path is None:
            return
        reset = _git(self.path, "reset", "--hard", "HEAD")
        clean = _git(self.path, "clean", "-fd")
        if reset.returncode != 0 or clean.returncode != 0:
            raise WorktreeIsolationError("could not reset isolated worktree for retry")

    def capture(self) -> WorktreePatch:
        if self.path is None:
            raise WorktreeIsolationError("isolated worktree is not active")
        staged = _git(self.path, "add", "-A")
        if staged.returncode != 0:
            raise WorktreeIsolationError(f"could not stage isolated changes: {_git_error(staged)}")
        names = _git(self.path, "diff", "--cached", "--name-only", "-z", "HEAD")
        if names.returncode != 0:
            raise WorktreeIsolationError(f"could not inspect isolated changes: {_git_error(names)}")
        changed = tuple(
            part.decode("utf-8", "surrogateescape")
            for part in names.stdout.split(b"\0")
            if part
        )
        outside = [path for path in changed if not _path_allowed(path, self.targets)]
        if outside:
            shown = ", ".join(repr(path) for path in outside[:4])
            raise WorktreeIsolationError(f"worker changed paths outside declared targets: {shown}")
        diff = _git(self.path, "diff", "--cached", "--binary", "--full-index", "HEAD")
        if diff.returncode != 0:
            raise WorktreeIsolationError(f"could not capture isolated patch: {_git_error(diff)}")
        return WorktreePatch(data=diff.stdout, changed_paths=changed)

    def _cleanup(self) -> None:
        if self.path is not None and self.path.exists():
            with contextlib.suppress(Exception):
                _git(self.root, "worktree", "remove", "--force", os.fspath(self.path))
        with contextlib.suppress(Exception):
            _git(self.root, "worktree", "prune")
        if self._temp_parent is not None:
            shutil.rmtree(self._temp_parent, ignore_errors=True)
        self.path = None
        self._temp_parent = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self._cleanup()


def preflight_patch(root: Path, patch: WorktreePatch) -> tuple[bool, str]:
    if not patch.data:
        return True, ""
    checked = _git(root, "apply", "--check", "--whitespace=nowarn", "-", input_bytes=patch.data)
    return checked.returncode == 0, ("" if checked.returncode == 0 else _git_error(checked))


def apply_patches(root: Path, patches: list[WorktreePatch]) -> tuple[bool, str]:
    """Apply a preflighted, non-overlapping wave as one patch operation."""
    payload = b"\n".join(patch.data for patch in patches if patch.data)
    if not payload:
        return True, ""
    applied = _git(root, "apply", "--whitespace=nowarn", "-", input_bytes=payload)
    return applied.returncode == 0, ("" if applied.returncode == 0 else _git_error(applied))
