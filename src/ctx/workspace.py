"""Workspace resolution, identity, and path confinement (SPEC §5).

Absolute paths never leave this module in model-visible form; digests and
search results always use repo-relative POSIX paths.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ctx import pathglob
from ctx.config import CONFIG_FILENAME, Config, load_config, load_ctxignore


class WorkspaceError(Exception):
    pass


class AmbiguousWorkspaceError(WorkspaceError):
    pass


class PathEscapeError(WorkspaceError):
    """A path or symlink resolves outside the workspace root (invariant 6)."""


@dataclass(frozen=True)
class GitInfo:
    head: str | None
    remote: str | None  # normalized remote identity


@dataclass
class Workspace:
    root: Path  # absolute, internal use only — never emitted in digests
    workspace_id: str
    config: Config
    ignore_globs: tuple[str, ...]
    git: GitInfo | None
    alias: str | None = None

    def git_dirty(self) -> bool | None:
        """Worktree dirty state, computed on demand (spawns one git process).
        Kept off the resolution hot path — retrieval never needs it."""
        if self.git is None:
            return None
        try:
            out = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.root,
                capture_output=True,
                timeout=15,
            )
            return bool(out.stdout) if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    # ---------------------------------------------------------------- paths
    def confine(self, rel_or_abs: str | Path, *, must_exist: bool = False) -> Path:
        """Resolve a path against the workspace root and refuse any escape
        via ``..`` or symlinks unless policy explicitly allows it."""
        p = Path(rel_or_abs)
        candidate = p if p.is_absolute() else self.root / p
        resolved = candidate.resolve()
        root = self.root.resolve()
        if not self.config.workspace.allow_outside_root:
            if not resolved.is_relative_to(root):  # S5 ADOPT #5 (stdlib ≥3.9)
                raise PathEscapeError(
                    f"path resolves outside the workspace: {self.relativize(p)!s}"
                )
            if not self.config.workspace.follow_symlinks:
                # Reject any symlinked component that points outside the root,
                # even when the final resolution lands back inside. Walk EVERY
                # component from the leaf up to (not including) the root — the
                # earlier `while not exists` walk skipped intermediate
                # directory symlinks whenever the full path already existed,
                # so a mid-path `evil -> /outside/back_inside` was never
                # inspected (bug bash S6 #3).
                root_abs = self.root.absolute()
                probe = candidate.absolute()
                while probe != probe.parent and probe != root_abs:
                    if probe.is_symlink():
                        # Check the IMMEDIATE target (one readlink hop),
                        # resolved relative to the link's own directory — not
                        # the fully-collapsed path. A hop that leaves the
                        # workspace is an escape even when a later hop lands
                        # back inside (bug bash S6 #3).
                        raw = Path(os.readlink(probe))
                        base = raw if raw.is_absolute() else (probe.parent / raw)
                        # Lexical normalization only (os.path.normpath) — do
                        # NOT .resolve(), which would follow the NEXT symlink
                        # and collapse an outside hop back inside.
                        hop = Path(os.path.normpath(base))
                        if hop != root and root not in hop.parents:
                            raise PathEscapeError(
                                f"symlink escapes the workspace: {self.relativize(probe)!s}"
                            )
                    probe = probe.parent
        if must_exist and not resolved.exists():
            raise WorkspaceError(f"no such path in workspace: {self.relativize(p)!s}")
        return resolved

    def relativize(self, p: str | Path) -> str:
        """Repo-relative POSIX path for model-visible output."""
        try:
            return Path(p).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return Path(p).name

    def is_ignored(self, rel_path: str) -> bool:
        rel = rel_path.removeprefix("./")
        spec = self._ignore_spec()
        if spec is not None:
            # Real gitignore semantics (anchoring, dir patterns, negation)
            # via pathspec — the same matcher Black uses.
            return spec.match_file(rel) or spec.match_file(rel + "/")
        return self._is_ignored_fnmatch(rel)

    def _ignore_spec(self):
        cached = getattr(self, "_ignore_spec_cache", False)
        if cached is not False:
            return cached
        try:
            import pathspec

            try:
                spec = pathspec.PathSpec.from_lines("gitignore", self.ignore_globs)
            except KeyError:  # older pathspec releases
                spec = pathspec.PathSpec.from_lines("gitwildmatch", self.ignore_globs)
        except Exception:
            spec = None  # stdlib fallback keeps the harness functional
        self._ignore_spec_cache = spec
        return spec

    def _is_ignored_fnmatch(self, rel: str) -> bool:
        """Fallback for a broken pathspec install -- same dialect, one engine.

        Named for the stdlib module it used to call directly. It no longer
        does: raw fnmatch lets ``*`` cross ``/``, so this fallback disagreed
        with the pathspec path above it about what an ignore glob covers,
        and the retrieval-side matcher was a third opinion again. All three
        now go through ctx.pathglob.
        """
        for glob in self.ignore_globs:
            if pathglob.matches(rel, glob):
                return True
            # Directory patterns like `**/secrets/**` should also match the
            # directory itself and paths under a bare-name pattern.
            if glob.endswith("/**") and pathglob.matches(rel, glob[: -len("/**")]):
                return True
        return False

    # ---------------------------------------------------------------- files
    def list_files(self, subtree: str | None = None) -> list[str]:
        """Repo-relative file listing, respecting .gitignore (via git when
        available) plus .ctxignore. Deterministically sorted."""
        base = self.confine(subtree or ".", must_exist=True)
        if base.is_file():
            # File selector (repo:<path-to-file>): the corpus is that file.
            rel = self.relativize(base)
            return [] if self.is_ignored(rel) else [rel]
        rels: list[str] = []
        if self.git is not None and self.config.workspace.respect_gitignore:
            try:
                out = subprocess.run(
                    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                    cwd=base,
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                prefix = "" if base == self.root else self.relativize(base) + "/"
                for name in out.stdout.decode("utf-8", "replace").split("\0"):
                    if name and (base / name).is_file():
                        rels.append(prefix + Path(name).as_posix())
            except (OSError, subprocess.SubprocessError):
                rels = self._walk(base)
        else:
            rels = self._walk(base)
        return sorted(r for r in rels if not self.is_ignored(r))

    def _walk(self, base: Path) -> list[str]:
        rels: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d != ".git" and not self.is_ignored(self.relativize(Path(dirpath) / d))
            )
            for fn in filenames:
                full = Path(dirpath) / fn
                if full.is_symlink() and not self.config.workspace.follow_symlinks:
                    continue
                rels.append(self.relativize(full))
        return rels


# ------------------------------------------------------- cache invalidation
def stat_fingerprint(root: Path | str, rels, h) -> None:
    """Fold the on-disk state of ``rels`` (repo-relative) into ``h``.

    The one stat-based cache-invalidation basis in the harness. Three caches
    (``repomap``, ``callgraph``, ``plan_exec``'s node cache) key on file
    metadata rather than content because hashing every file on every lookup
    is the thing they exist to avoid; ``skeleton`` keys on the source blob
    hash and deliberately does not use this — content is the stronger basis
    and it already has the hash in hand.

    The basis is ``(rel, size, mtime_ns, ctime_ns)``. ``ctime_ns`` is not
    redundant: mtime is settable from userspace, so a same-length edit whose
    mtime is put back (``os.utime``, ``rsync -t``, ``tar -p``, editors that
    save-and-restore timestamps) is invisible to size+mtime alone and used
    to serve a stale map/graph/node result. ctime is bumped by the write and
    by the utime call itself and cannot be moved backwards.

    Unstattable paths are skipped (a deleted file leaves the listing that
    produced ``rels`` anyway); the traversal order is the caller's, so the
    caller owns determinism.
    """
    base = Path(root)
    for rel in rels:
        try:
            st = (base / rel).stat()
        except OSError:
            continue
        h.update(
            f"{rel}|{st.st_size}|{st.st_mtime_ns}|{st.st_ctime_ns}\n".encode("utf-8")
        )


# ------------------------------------------------------------------ identity
def _git_toplevel(path: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            timeout=10,
        )
        if out.returncode == 0:
            top = out.stdout.decode().strip()
            if top:
                return Path(top)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _git_dir(root: Path) -> Path | None:
    git_path = root / ".git"
    if git_path.is_dir():
        return git_path
    if git_path.is_file():  # worktree / submodule gitdir pointer
        try:
            content = git_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if content.startswith("gitdir: "):
            target = Path(content[len("gitdir: ") :])
            return target if target.is_absolute() else (root / target).resolve()
    return None


def _git_info(root: Path) -> GitInfo | None:
    """Latency-critical path: git identity from direct file reads — HEAD,
    refs, packed-refs, and config — with zero subprocess spawns."""
    git_dir = _git_dir(root)
    if git_dir is None:
        return None

    head: str | None = None
    try:
        head_content = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head_content.startswith("ref: "):
            ref = head_content[len("ref: ") :]
            ref_path = git_dir / ref
            if ref_path.is_file():
                head = ref_path.read_text(encoding="utf-8").strip() or None
            else:
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        if line.endswith(" " + ref):
                            head = line.split(" ", 1)[0]
                            break
        elif len(head_content) >= 40:
            head = head_content  # detached HEAD
    except (OSError, UnicodeDecodeError):
        pass

    remote: str | None = None
    try:
        in_origin = False
        for line in (git_dir / "config").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("["):
                in_origin = line.replace(" ", "") in ('[remote"origin"]',)
            elif in_origin and line.startswith("url"):
                _, _, value = line.partition("=")
                remote = normalize_remote(value.strip())
                break
    except (OSError, UnicodeDecodeError):
        pass

    return GitInfo(head=head, remote=remote)


def normalize_remote(url: str) -> str:
    """Normalize git remote identity: strip scheme, credentials, and .git."""
    u = url.strip()
    if u.startswith("git@"):
        u = u[len("git@") :].replace(":", "/", 1)
    for scheme in ("https://", "http://", "ssh://", "git://"):
        if u.startswith(scheme):
            u = u[len(scheme) :]
    if "@" in u.split("/", 1)[0]:
        u = u.split("@", 1)[1]
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u.lower()


def stable_workspace_id(root: Path, config: Config, git: GitInfo | None) -> str:
    """Opaque workspace id. With a committed ``repo_key`` (or a normalized
    remote) the id is stable across clones; otherwise it derives from the
    local root path but is never itself emitted as a path."""
    seed = config.repo_key or (git.remote if git else None) or str(root.resolve())
    return "ws_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------- resolver
def resolve_workspace(
    explicit: str | None = None,
    *,
    cwd: Path | None = None,
    hook_workspace_paths: list[str] | None = None,
    target_path: str | None = None,
) -> Workspace:
    """SPEC §5.1 resolution order:

    1. explicit ``--workspace``;
    2. longest containing hook workspacePath vs target/cwd;
    3. nearest ancestor containing ``ctx.toml``;
    4. ``git rev-parse --show-toplevel``;
    5. nearest ancestor containing ``.agents/``;
    6. cwd as plain-folder workspace.
    """
    cwd = (cwd or Path.cwd()).resolve()

    root: Path | None = None
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceError(f"--workspace is not a directory: {explicit}")
    elif hook_workspace_paths:
        probe = Path(target_path).expanduser().resolve() if target_path else cwd
        candidates = []
        for wp in hook_workspace_paths:
            wpr = Path(wp).expanduser().resolve()
            if wpr == probe or wpr in probe.parents:
                candidates.append(wpr)
        if candidates:
            root = max(candidates, key=lambda p: len(p.parts))
        elif len(hook_workspace_paths) == 1:
            root = Path(hook_workspace_paths[0]).expanduser().resolve()
        else:
            raise AmbiguousWorkspaceError(
                "multiple workspaces are plausible; pass --workspace or ws:<alias>"
            )

    if root is None:
        for anc in [cwd, *cwd.parents]:
            if (anc / CONFIG_FILENAME).is_file():
                root = anc
                break
    if root is None:
        root = _git_toplevel(cwd)
    if root is None:
        for anc in [cwd, *cwd.parents]:
            if (anc / ".agents").is_dir():
                root = anc
                break
    if root is None:
        root = cwd

    config = load_config(root)
    git = _git_info(root)
    return Workspace(
        root=root,
        workspace_id=stable_workspace_id(root, config, git),
        config=config,
        ignore_globs=load_ctxignore(root),
        git=git,
    )
