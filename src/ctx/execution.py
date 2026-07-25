"""Birth-time capture runner (SPEC §6.2, §7).

Output is spooled to disk as it streams — it never accumulates in process
memory and never reaches the model before it is content-addressed.
"""

from __future__ import annotations

import hashlib
import os
import signal as signal_mod
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctx.gitstatus import changed_paths
from ctx.store import Store
from ctx.textutil import decode_stream
from ctx.workspace import Workspace


class ExecutionError(Exception):
    pass


@dataclass
class CaptureResult:
    manifest_id: str
    manifest: dict[str, Any]


def _count_lines(path: Path) -> int:
    lines = 0
    last = b"\n"
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last not in (b"\n", b""):
        lines += 1
    return lines


def _normalize_focus(focus: str | None) -> str:
    if not focus:
        return ""
    return " ".join(focus.lower().split())


def focus_hash(focus: str | None) -> str:
    return "sha256:" + hashlib.sha256(_normalize_focus(focus).encode("utf-8")).hexdigest()


def run_capture(
    ws: Workspace,
    argv: list[str],
    *,
    cwd: str | None = None,
    shell: bool = False,
    timeout: float | None = 600.0,
    store: Store | None = None,
    stdin_bytes: bytes | None = None,
    record_argv: list[str] | None = None,
) -> CaptureResult:
    """Execute a command, streaming stdout/stderr into distinct immutable
    blobs, and publish a ``ctx.invocation/v1`` manifest.

    ``stdin_bytes`` is spooled to disk and fed as the child's stdin (never a
    pipe, so no deadlock and no size limit). ``record_argv`` substitutes the
    manifest's model-visible argv when the executed argv carries a
    host-specific absolute path (e.g. ``sys.executable``) that must never
    appear in manifests or digests.
    """
    if not argv:
        raise ExecutionError("empty command")
    store = store or Store(ws.workspace_id)

    workdir = ws.confine(cwd or ".", must_exist=True)
    rel_cwd = ws.relativize(workdir) or "."

    if shell:
        if len(argv) != 1:
            raise ExecutionError("--shell mode takes exactly one command string")
        popen_args: Any = argv[0]
    else:
        popen_args = argv

    tmpdir = Path(tempfile.mkdtemp(prefix="ctx-cap-"))
    out_path = tmpdir / "stdout"
    err_path = tmpdir / "stderr"
    in_path: Path | None = None
    if stdin_bytes is not None:
        in_path = tmpdir / "stdin"
        in_path.write_bytes(stdin_bytes)
    timed_out = False
    try:
        in_fh = in_path.open("rb") if in_path is not None else None
        with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
            try:
                proc = subprocess.Popen(
                    popen_args,
                    cwd=workdir,
                    shell=shell,
                    stdout=out_fh,
                    stderr=err_fh,
                    stdin=in_fh if in_fh is not None else subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError as e:
                raise ExecutionError(f"command not found: {argv[0]}") from e
            finally:
                if in_fh is not None:
                    in_fh.close()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal_mod.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()

        exit_code: int | None = proc.returncode
        sig_name: str | None = None
        if exit_code is not None and exit_code < 0:
            try:
                sig_name = signal_mod.Signals(-exit_code).name
            except ValueError:
                sig_name = f"SIG{-exit_code}"
            exit_code = None

        streams: dict[str, dict[str, Any]] = {}
        for name, path in (("stdout", out_path), ("stderr", err_path)):
            blob_hash, size = store.put_blob_from_file(path)
            head = path.open("rb").read(8192)
            _, encoding, media_type = decode_stream(head if size else b"")
            streams[name] = {
                "blob": f"sha256:{blob_hash}",
                "bytes": size,
                "lines": _count_lines(path),
                "mediaType": media_type if size else "text/plain",
                "encoding": encoding if size else "utf-8",
            }
    finally:
        for p in (out_path, err_path) + ((in_path,) if in_path is not None else ()):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass

    manifest: dict[str, Any] = {
        "schema": "ctx.invocation/v1",
        "workspaceId": ws.workspace_id,
        "cwd": rel_cwd,
        "argv": list(record_argv if record_argv is not None else argv),
        "shell": shell,
        "result": {
            "exitCode": exit_code,
            "signal": sig_name,
            "timedOut": timed_out,
        },
        "streams": streams,
        "source": {
            "gitHead": ws.git.head if ws.git else None,
            "worktreeHash": _worktree_hash(ws),
        },
        # digest fields are filled by the digest layer after profile
        # selection; placeholders keep the schema shape stable.
        "digest": {
            "profile": "text/v1",
            "policy": "default/v1",
            "focusHash": focus_hash(None),
            "bytesHash": "sha256:" + "0" * 64,
        },
    }
    manifest_id = store.put_manifest(manifest, kind="run")
    manifest["id"] = f"sha256:{manifest_id}"
    return CaptureResult(manifest_id=manifest_id, manifest=manifest)


def _worktree_hash(ws: Workspace) -> str | None:
    """Stable hash of dirty-state summary; None for non-git workspaces."""
    if ws.git is None:
        return None
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ws.root,
            capture_output=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        return "sha256:" + hashlib.sha256(out.stdout).hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


# Bookkeeping directory excluded from the generation walk: the reflex/session
# ledgers mutate on every scored command, so including them would bump the
# generation on our own writes and confirm nothing, ever.
_GENERATION_EXCLUDE_DIR = ".ctx-session-reads"
# Bound on the untracked-file walk. Ignored trees (node_modules, venvs) never
# appear in porcelain, so real workspaces sit far below this; the cap only
# guards pathological unignored trees. Deterministic: the walk is sorted, and
# hitting the cap folds the total count into the hash instead of the tail.
_GENERATION_MAX_UNTRACKED = 4096


def generation_hash(ws_root: Any) -> str | None:
    """Source-state generation (docs/EDC.md §8): the operational identity of
    the worktree at a scoring moment.

    ``sha256`` over the raw ``git status --porcelain`` bytes PLUS, for every
    untracked file (each ``?? `` entry per :mod:`ctx.gitstatus`, recursed
    through untracked directories), its ``(relative path, size, mtime_ns)``
    triple in sorted
    path order. The untracked triples are the §8.2 fix for the
    untracked-content trap: porcelain lists ``?? file`` regardless of
    content, so edits to just-created unstaged files (the dominant
    spec-driven-creation pattern — exactly the spec3 workload) never change
    :func:`_worktree_hash` and would confirm false starvations. Size+mtime_ns
    is legal here because generations are OPERATIONAL identity (did the
    source plausibly change between two runs?), never content identity —
    manifest identity stays :func:`_worktree_hash`, unchanged.

    Hot-path discipline: callers (the reflex arc) invoke this LAZILY, only at
    scoring moments (an intervention being recorded, an equivalent/narrower
    rerun being classified) — never per command. Fail-open by contract:
    non-git roots, git errors, and IO problems all return None (unknown
    generation), never raise.
    """
    if ws_root is None:
        return None
    try:
        root = Path(ws_root)
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        h = hashlib.sha256(out.stdout)
        untracked: list[Path] = []
        rels = changed_paths(
            out.stdout, untracked_only=True, exclude_top=_GENERATION_EXCLUDE_DIR
        )
        for rel in rels:
            p = root / rel
            if rel.endswith("/") or p.is_dir():
                # Porcelain lists an untracked directory as ONE entry; walk
                # it, or edits inside would be invisible to the generation.
                for sub in sorted(p.rglob("*")):
                    if sub.is_file():
                        untracked.append(sub)
            elif p.is_file():
                untracked.append(p)
        for p in sorted(untracked)[:_GENERATION_MAX_UNTRACKED]:
            try:
                rel_str = str(p.relative_to(root))
            except ValueError:
                rel_str = str(p)
            try:
                st = p.stat()
                h.update(
                    f"\x00{rel_str}\x00{st.st_size}\x00{st.st_mtime_ns}".encode(
                        "utf-8", "replace"
                    )
                )
            except OSError:
                h.update(f"\x00{rel_str}\x00gone".encode("utf-8", "replace"))
        if len(untracked) > _GENERATION_MAX_UNTRACKED:
            h.update(f"\x00capped:{len(untracked)}".encode("utf-8"))
        return "sha256:" + h.hexdigest()
    except Exception:
        return None


def update_manifest_digest(
    store: Store, manifest: dict[str, Any], digest_meta: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    """Publish the final manifest with its digest identity. Content identity
    covers artifact bytes + normalized invocation + profile/policy/focus."""
    body = {k: v for k, v in manifest.items() if k != "id"}
    body["digest"] = digest_meta
    new_id = store.put_manifest(body, kind="run")
    body["id"] = f"sha256:{new_id}"
    return new_id, body


def snapshot_file(store: Store, ws: Workspace, rel_path: str) -> dict[str, Any]:
    """Snapshot-on-read: pin the current bytes of a workspace file so later
    ``get`` operations remain stable even if the working tree changes."""
    full = ws.confine(rel_path, must_exist=True)
    if not full.is_file():
        raise ExecutionError(f"not a file: {rel_path}")
    if ws.is_ignored(ws.relativize(full)):
        raise ExecutionError(
            f"path is excluded from capture by policy: {ws.relativize(full)}"
        )
    data = full.read_bytes()
    blob_hash = store.put_blob(data)
    _, encoding, media_type = decode_stream(data[:8192] if data else b"")
    manifest = {
        "schema": "ctx.snapshot/v1",
        "workspaceId": ws.workspace_id,
        "path": ws.relativize(full),
        "blob": f"sha256:{blob_hash}",
        "bytes": len(data),
        "lines": data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1),
        "mediaType": media_type if data else "text/plain",
        "encoding": encoding if data else "utf-8",
        "source": {"gitHead": ws.git.head if ws.git else None},
    }
    mid = store.put_manifest(manifest, kind="snapshot")
    manifest["id"] = f"sha256:{mid}"
    return manifest
