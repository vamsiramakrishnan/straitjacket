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
import re
import shutil
import subprocess
import tempfile
import threading
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
            or not path.parts  # "." / "./": pathlib normalizes them to ()
            or path.is_absolute()
            or ".." in path.parts
            or path.parts[0] == ".git"
        ):
            raise WorktreeIsolationError(
                f"unsafe declared target: {raw!r}"
                + (" (a scope names paths; '.' is the whole repository)"
                   if value and not path.parts else "")
            )
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


#: `git worktree add` / `remove` / `prune` all rewrite `.git/worktrees/` in
#: the shared repository. Parallel wave nodes create their worktrees at the
#: same moment, and git's own guard against a colliding entry is a check-
#: then-create with no lock -- CI saw one node read the other's half-written
#: entry ("failed to read .git/worktrees/repo/commondir"). One process-wide
#: lock around the three mutating commands closes that window for the
#: orchestrator's own threads; the unique leaf name below closes the
#: collision itself.
_WORKTREE_LOCK = threading.Lock()
_NODE_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


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
        # git derives the worktree's id from the leaf directory name. Every
        # checkout used to be `<tmp>/repo`, so two nodes adding at once both
        # asked for the id "repo" and raced on the same `.git/worktrees/repo`
        # entry. The leaf now carries the node id and the temp dir's own
        # unique suffix, so ids never collide.
        node = _NODE_SAFE.sub("-", self.node_id).strip("-") or "node"
        suffix = self._temp_parent.name.removeprefix("ctx-worktree-")
        self.path = self._temp_parent / f"{node}-{suffix}"
        with _WORKTREE_LOCK:
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
        with _WORKTREE_LOCK:
            if self.path is not None and self.path.exists():
                with contextlib.suppress(Exception):
                    # --force twice: a single --force still refuses a locked worktree.
                    _git(self.root, "worktree", "remove", "--force", "--force", os.fspath(self.path))
            if self._temp_parent is not None:
                shutil.rmtree(self._temp_parent, ignore_errors=True)
            # prune after rmtree: if `remove` failed (e.g. locked), the directory
            # still existed when prune ran here before, so it found nothing to reap.
            with contextlib.suppress(Exception):
                _git(self.root, "worktree", "prune")
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


class PersistentWorktree:
    """An owned checkout that survives a task process and exposes guarded patches.

    Lifecycle is explicit; completed work is retained for review. This is also
    usable by SDK workflows unrelated to the investigation controller.
    """
    def __init__(self, root: Path, path: Path, base: str, *, output_bytes=8 * 1024 * 1024):
        self.root, self.path, self.base = root.resolve(), path.resolve(), base
        self.output_bytes = output_bytes

    def git(self, *args, input_bytes=b"", root=None):
        from ctx.semantic.worker import CommandWorker
        result = CommandWorker(["git", *args], cwd=root or self.path)(input_bytes, timeout=30,
                                                                               response_bytes=self.output_bytes)
        if result.error or result.returncode != 0:
            raise WorktreeIsolationError("worktree git operation failed: " + (result.error or "nonzero_exit"))
        return result.stdout

    def open(self):
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _WORKTREE_LOCK:
                self.git("worktree", "add", "--detach", str(self.path), self.base, root=self.root)
        head = self.git("rev-parse", "HEAD").decode().strip()
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        expected = self.git("rev-parse", "--path-format=absolute", "--git-common-dir", root=self.root).decode().strip()
        if head != self.base or Path(common).resolve() != Path(expected).resolve():
            raise WorktreeIsolationError("retained checkout belongs to another base or repository")
        return self

    def fingerprint(self):
        import hashlib
        from ctx.sessiondir import LEDGER_DIR_NAME
        head = self.git("rev-parse", "HEAD")
        patch = self.git("diff", "--binary", "--full-index", "HEAD", "--", ".", ":(exclude)" + LEDGER_DIR_NAME)
        untracked = self.git("ls-files", "--others", "--exclude-standard", "-z")
        extra = bytearray()
        for raw in untracked.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8")
            if rel == LEDGER_DIR_NAME or rel.startswith(LEDGER_DIR_NAME + "/"):
                continue
            path = self.path / rel
            if path.is_symlink() or not path.is_file() or path.stat().st_size > self.output_bytes:
                raise WorktreeIsolationError("unsupported untracked worktree entry")
            extra.extend(raw + b"\0" + hashlib.sha256(path.read_bytes()).digest())
        return hashlib.sha256(head + patch + bytes(extra)).hexdigest()

    def capture(self, targets):
        names = self.git("diff", "--name-only", "-z", "HEAD")
        changed = tuple(p.decode("utf-8") for p in names.split(b"\0") if p)
        if not all("." in targets or _path_allowed(p, tuple(targets)) for p in changed):
            raise WorktreeIsolationError("patch changes files outside the declared targets")
        data = self.git("diff", "--binary", "--full-index", "HEAD")
        return WorktreePatch(data, changed)
