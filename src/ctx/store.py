"""Content-addressed artifact store (SPEC §12).

Layout (outside the repository by default):

    <XDG_STATE_HOME>/ctx/workspaces/<workspace-id>/
        blobs/sha256/ab/cdef...      immutable content
        manifests/<sha256>.json      invocation / snapshot manifests
        indexes/catalog.sqlite3      short-id lookup, kinds, leases (WAL)
        audit/                       operational telemetry, non-identity

All writes are atomic (temp file + rename); a crash never leaves a partially
published manifest. Operational metadata (timestamps, leases) lives in the
catalog and never participates in content identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_ID_DISPLAY = 12


class StoreError(Exception):
    pass


class AmbiguousIdError(StoreError):
    def __init__(self, short: str, candidates: list[str]):
        self.candidates = candidates
        joined = "\n  ".join(candidates)
        super().__init__(
            f"ambiguous short id {short!r}; candidates:\n  {joined}\nuse a longer prefix"
        )


class UnknownIdError(StoreError):
    pass


def default_state_root() -> Path:
    env = os.environ.get("CTX_STATE_HOME")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "ctx"


def canonical_json(obj: Any) -> bytes:
    """Canonical serialization: sorted keys, no volatile formatting."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id TEXT PRIMARY KEY,          -- full sha256 hex
    kind TEXT NOT NULL,           -- run | blob | snapshot | search | checkpoint
    created_at REAL NOT NULL,     -- operational only, never in content identity
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind);
CREATE TABLE IF NOT EXISTS leases (
    id TEXT NOT NULL,
    reason TEXT NOT NULL,         -- retention | pin | checkpoint
    expires_at REAL,              -- NULL = pinned forever
    PRIMARY KEY (id, reason)
);
CREATE TABLE IF NOT EXISTS turn_usage (
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, turn_id)
);
"""


@dataclass(frozen=True)
class StoredObject:
    id: str
    kind: str
    meta: dict[str, Any]


class Store:
    """Per-workspace artifact store. Handles are scoped to the workspace
    (SPEC §12.2); raw store paths are never emitted to the model."""

    def __init__(self, workspace_id: str, state_root: Path | None = None):
        self.workspace_id = workspace_id
        self.root = (state_root or default_state_root()) / "workspaces" / workspace_id
        self.blob_dir = self.root / "blobs" / "sha256"
        self.manifest_dir = self.root / "manifests"
        self.audit_dir = self.root / "audit"
        for d in (self.blob_dir, self.manifest_dir, self.root / "indexes", self.audit_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._db_path = self.root / "indexes" / "catalog.sqlite3"
        self._db: sqlite3.Connection | None = None

    # ------------------------------------------------------------- catalog
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._db = conn
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _register(self, obj_id: str, kind: str, meta: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO objects (id, kind, created_at, meta) VALUES (?,?,?,?)",
                (obj_id, kind, time.time(), json.dumps(meta, sort_keys=True)),
            )

    # --------------------------------------------------------------- blobs
    def blob_path(self, blob_hash: str) -> Path:
        return self.blob_dir / blob_hash[:2] / blob_hash[2:]

    def put_blob(self, data: bytes) -> str:
        h = hashlib.sha256(data).hexdigest()
        path = self.blob_path(h)
        if not path.exists():
            _atomic_write(path, data)
        self._register(h, "blob", {"bytes": len(data)})
        return h

    def put_blob_from_file(self, src: Path) -> tuple[str, int]:
        """Stream-hash a spooled capture file into the blob store."""
        hasher = hashlib.sha256()
        size = 0
        with src.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                hasher.update(chunk)
                size += len(chunk)
        h = hasher.hexdigest()
        path = self.blob_path(h)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            os.close(fd)
            try:
                import shutil

                shutil.copyfile(src, tmp)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        self._register(h, "blob", {"bytes": size})
        return h, size

    def get_blob(self, blob_hash: str) -> bytes:
        path = self.blob_path(self.resolve_id(blob_hash, kinds=("blob",)))
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise UnknownIdError(f"blob:{blob_hash[:MIN_ID_DISPLAY]} not found") from None

    # ----------------------------------------------------------- manifests
    def put_manifest(self, manifest: dict[str, Any], kind: str) -> str:
        """Content-address a manifest. The ``id`` field is derived from the
        canonical bytes of the manifest without it."""
        body = {k: v for k, v in manifest.items() if k != "id"}
        h = hashlib.sha256(canonical_json(body)).hexdigest()
        manifest = dict(body)
        manifest["id"] = f"sha256:{h}"
        _atomic_write(self.manifest_dir / f"{h}.json", canonical_json(manifest))
        self._register(h, kind, {})
        # Retention lease so gc keeps recent artifacts (SPEC §12.3).
        self.lease(h, "retention", ttl_days=None)
        return h

    def get_manifest(self, manifest_id: str) -> dict[str, Any]:
        full = self.resolve_id(manifest_id)
        path = self.manifest_dir / f"{full}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise UnknownIdError(f"manifest {manifest_id[:MIN_ID_DISPLAY]} not found") from None

    # -------------------------------------------------------- line indexes
    def line_index(self, blob_hash: str) -> "array.array":
        """Byte offsets of line starts for a blob, built lazily and cached on
        disk. Enables O(1) line slicing without decoding the whole blob."""
        import array

        blob_hash = blob_hash.removeprefix("sha256:")
        idx_path = self.root / "indexes" / "lines" / blob_hash[:2] / (blob_hash[2:] + ".idx")
        arr = array.array("Q")
        if idx_path.is_file():
            arr.frombytes(idx_path.read_bytes())
            return arr
        data = self.get_blob(blob_hash)
        arr.append(0)
        pos = data.find(b"\n")
        while pos != -1:
            arr.append(pos + 1)
            pos = data.find(b"\n", pos + 1)
        if arr[-1] != len(data):
            arr.append(len(data))  # sentinel: end of final unterminated line
        _atomic_write(idx_path, arr.tobytes())
        return arr

    def read_blob_lines(self, blob_hash: str, start: int, end: int) -> bytes:
        """Read lines [start, end] (1-indexed, inclusive) via the line index,
        touching only the needed byte range of the blob file."""
        blob_hash = blob_hash.removeprefix("sha256:")
        idx = self.line_index(blob_hash)
        n_lines = max(0, len(idx) - 1)
        if n_lines == 0 or start > n_lines:
            return b""
        start = max(1, start)
        end = min(end, n_lines)
        with self.blob_path(self.resolve_id(blob_hash, kinds=("blob",))).open("rb") as fh:
            fh.seek(idx[start - 1])
            return fh.read(idx[end] - idx[start - 1])

    # ------------------------------------------------------------- lookups
    def resolve_id(self, short: str, kinds: tuple[str, ...] | None = None) -> str:
        """Expand a short id; refuse ambiguity (SPEC §6.1)."""
        short = short.removeprefix("sha256:").lower()
        if len(short) == 64:
            return short
        if len(short) < 6:
            raise StoreError(f"id prefix too short: {short!r} (need ≥6 hex chars)")
        if kinds:
            q = f"SELECT id FROM objects WHERE id LIKE ? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id"
            rows = self.db.execute(q, (short + "%", *kinds)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id FROM objects WHERE id LIKE ? ORDER BY id", (short + "%",)
            ).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            raise UnknownIdError(f"no object matches id prefix {short!r} in this workspace")
        if len(ids) > 1:
            raise AmbiguousIdError(short, ids)
        return ids[0]

    def kind_of(self, obj_id: str) -> str:
        row = self.db.execute("SELECT kind FROM objects WHERE id=?", (obj_id,)).fetchone()
        if row is None:
            raise UnknownIdError(f"unknown object {obj_id[:MIN_ID_DISPLAY]}")
        return row[0]

    # -------------------------------------------------------------- leases
    def lease(self, obj_id: str, reason: str, ttl_days: int | None) -> None:
        expires = None if ttl_days is None else time.time() + ttl_days * 86400
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO leases (id, reason, expires_at) VALUES (?,?,?)",
                (obj_id, reason, expires),
            )

    def pin(self, obj_id: str) -> None:
        self.lease(self.resolve_id(obj_id), "pin", ttl_days=None)

    def gc(self, retention_days: int) -> dict[str, int]:
        """Mark-and-sweep over leases and pins. Blobs referenced by any live
        manifest survive. Never touches objects with a pin."""
        now = time.time()
        cutoff = now - retention_days * 86400
        live: set[str] = set()
        pinned = {
            r[0]
            for r in self.db.execute(
                "SELECT id FROM leases WHERE reason='pin' AND expires_at IS NULL"
            )
        }
        recent = {
            r[0]
            for r in self.db.execute("SELECT id FROM objects WHERE created_at >= ?", (cutoff,))
        }
        live |= pinned | recent
        # Mark blobs referenced by live manifests.
        for mid in list(live):
            mpath = self.manifest_dir / f"{mid}.json"
            if mpath.is_file():
                try:
                    manifest = json.loads(mpath.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for stream in (manifest.get("streams") or {}).values():
                    blob = str(stream.get("blob", "")).removeprefix("sha256:")
                    if blob:
                        live.add(blob)
                blob = str(manifest.get("blob", "")).removeprefix("sha256:")
                if blob:
                    live.add(blob)
        removed_blobs = removed_manifests = 0
        for row in self.db.execute("SELECT id, kind FROM objects").fetchall():
            obj_id, kind = row
            if obj_id in live:
                continue
            if kind == "blob":
                p = self.blob_path(obj_id)
                if p.exists():
                    p.unlink()
                removed_blobs += 1
            else:
                p = self.manifest_dir / f"{obj_id}.json"
                if p.exists():
                    p.unlink()
                removed_manifests += 1
            with self.db:
                self.db.execute("DELETE FROM objects WHERE id=?", (obj_id,))
                self.db.execute("DELETE FROM leases WHERE id=?", (obj_id,))
        return {"blobs_removed": removed_blobs, "manifests_removed": removed_manifests}

    # ----------------------------------------------------- per-turn budget
    def add_turn_tokens(self, conversation_id: str, turn_id: str, tokens: int) -> int:
        """Track cumulative retrieval tokens for a turn; returns new total."""
        with self.db:
            self.db.execute(
                "INSERT INTO turn_usage (conversation_id, turn_id, tokens) VALUES (?,?,?) "
                "ON CONFLICT(conversation_id, turn_id) DO UPDATE SET tokens = tokens + ?",
                (conversation_id, turn_id, tokens, tokens),
            )
        row = self.db.execute(
            "SELECT tokens FROM turn_usage WHERE conversation_id=? AND turn_id=?",
            (conversation_id, turn_id),
        ).fetchone()
        return int(row[0])
