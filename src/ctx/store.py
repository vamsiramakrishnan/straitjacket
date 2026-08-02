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

from ctx import bounds

import array
import hashlib
import json
import re
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

MIN_ID_DISPLAY = 12

# sha256 hex alphabet — used to gate the resolve_id() range-scan fast path
# (see resolve_id docstring for why a plain LIKE query can't use the index).
_HEX_DIGITS = frozenset("0123456789abcdef")



_BLOB_ID_RE = re.compile(r"\b(?:sha256:)?([0-9a-f]{64})\b")


def _referenced_blobs(node: object, _depth: int = 0) -> set[str]:
    """Every blob id reachable anywhere in a manifest document.

    Structural rather than schema-aware on purpose: a collector that learns
    each manifest kind's blob fields by hand is one new kind away from
    deleting live data, which is exactly what happened to
    ``ctx.investigation/v1``. Depth-bounded so a pathological document cannot
    turn the mark phase into a stack overflow.
    """
    found: set[str] = set()
    if _depth > 16:
        return found
    if isinstance(node, str):
        found.update(_BLOB_ID_RE.findall(node))
    elif isinstance(node, dict):
        for v in node.values():
            found |= _referenced_blobs(v, _depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            found |= _referenced_blobs(v, _depth + 1)
    return found


class StoreError(Exception):
    pass


# How many candidate ids an ambiguity message may name. This message is read
# by an agent, in a tool whose entire purpose is bounding what a model reads:
# every candidate is 64 hex characters, and a 6-character prefix over a large
# catalog can match hundreds, so the uncapped version was an unbounded flood
# emitted BY the flood guard. Enough to disambiguate by eye, never a page.
MAX_AMBIGUOUS_CANDIDATES = 8


class AmbiguousIdError(StoreError):
    def __init__(self, short: str, candidates: list[str]):
        self.candidates = candidates  # full list stays available to callers
        shown = candidates[:MAX_AMBIGUOUS_CANDIDATES]
        joined = "\n  ".join(shown)
        if len(candidates) > len(shown):
            joined += f"\n  … and {len(candidates) - len(shown)} more"
        super().__init__(
            f"ambiguous short id {short!r}; {len(candidates)} candidates:\n  "
            f"{joined}\nuse a longer prefix"
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
-- Every catalog read of a *kind* is really "the newest N of this kind":
--     SELECT id FROM objects WHERE kind='run' ORDER BY created_at DESC, id LIMIT ?
-- (facts.py, policy.py, repomap.py, installer.py). A plain objects(kind)
-- index answers only the WHERE half: SQLite then sorts every matching row
-- in a temp b-tree just to take the first few. The composite carries the
-- sort order *and* the selected column, so the same query becomes a
-- covering-index seek that stops at the LIMIT. Measured on a 50k-object
-- catalog (4,107 runs, SQLite 3.45.1, EXPLAIN QUERY PLAN in
-- tests/test_store_perf.py):
--     SEARCH objects USING INDEX objects_kind (kind=?) + TEMP B-TREE FOR ORDER BY
--         → 2.213 ms (LIMIT 40) / 2.017 ms (LIMIT 1)
--     SEARCH objects USING COVERING INDEX objects_kind_recent (kind=?)
--         → 0.012 ms (LIMIT 40) / 0.002 ms (LIMIT 1)
-- objects(kind) is a strict prefix of this index, so it is now redundant:
-- every plan it could serve, the composite serves at least as well (the
-- planner picked the composite even while both existed). Keeping both cost
-- 50.4 vs 44.6 us per single-row INSERT and ~0.7 MB per 50k objects, so the
-- old one is dropped. Dropping an index destroys no data and is reversible
-- (an older ctx reopening the same file just recreates it).
CREATE INDEX IF NOT EXISTS objects_kind_recent ON objects(kind, created_at DESC, id);
DROP INDEX IF EXISTS objects_kind;
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
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,     -- deterministic: sha256(blob|kind|params)[:10]
    blob TEXT NOT NULL,           -- full blob hash the span addresses
    kind TEXT NOT NULL,           -- region | template
    a INTEGER,                    -- region: first line (1-indexed)
    b INTEGER,                    -- region: last line (inclusive)
    template TEXT,                -- template: masked template string
    note TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    """Per-workspace artifact store. Handles are scoped to the workspace
    (SPEC §12.2); raw store paths are never emitted to the model."""

    def __init__(
        self,
        workspace_id: str,
        state_root: Path | None = None,
        retention_days: int = 30,
    ):
        self.workspace_id = workspace_id
        self.retention_days = retention_days
        self.root = (state_root or default_state_root()) / "workspaces" / workspace_id
        self.blob_dir = self.root / "blobs" / "sha256"
        self.manifest_dir = self.root / "manifests"
        self.audit_dir = self.root / "audit"
        for d in (self.blob_dir, self.manifest_dir, self.root / "indexes", self.audit_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._db_path = self.root / "indexes" / "catalog.sqlite3"
        self._db: sqlite3.Connection | None = None
        # In-process cache of parsed line-index arrays, keyed by full blob
        # hash. Safe because blobs are immutable/content-addressed and the
        # index is a pure function of blob bytes — measured: repeatedly
        # calling line_index()/read_blob_lines() on the same blob within one
        # process was re-reading and re-parsing the on-disk .idx sidecar
        # every time (~520-620 us/call on a 20 MB blob's index, dominating
        # the whole read_blob_lines call), even though nothing had changed.
        self._line_index_cache: dict[str, array.array] = {}

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
        # Time-bounded retention lease (SPEC §12.3): keeps the artifact alive
        # for the retention window; pins/checkpoints extend indefinitely.
        self.lease(h, "retention", ttl_days=self.retention_days)
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
        disk (plus an in-process cache; see ``_line_index_cache`` above).
        Enables O(1) line slicing without decoding the whole blob."""
        blob_hash = blob_hash.removeprefix("sha256:")
        cached = self._line_index_cache.get(blob_hash)
        if cached is not None:
            return cached
        idx_path = self.root / "indexes" / "lines" / blob_hash[:2] / (blob_hash[2:] + ".idx")
        arr = array.array("Q")
        if idx_path.is_file():
            arr.frombytes(idx_path.read_bytes())
            self._line_index_cache[blob_hash] = arr
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
        self._line_index_cache[blob_hash] = arr
        return arr

    def read_blob_lines(self, blob_hash: str, start: int, end: int) -> bytes:
        """Read lines [start, end] (1-indexed, inclusive) via the line index,
        touching only the needed byte range of the blob file."""
        blob_hash = blob_hash.removeprefix("sha256:")
        idx = self.line_index(blob_hash)
        n_lines = max(0, len(idx) - 1)
        # bounds.span, not min(end, n_lines): a negative `end` survived that
        # clamp and idx[end] wrapped around to dump most of the blob
        # (ctx.bounds). An empty span is empty, never a suffix.
        window = bounds.span(start, end, n_lines)
        if window is None:
            return b""
        start, end = window
        with self.blob_path(self.resolve_id(blob_hash, kinds=("blob",))).open("rb") as fh:
            fh.seek(idx[start - 1])
            return fh.read(idx[end] - idx[start - 1])

    # ------------------------------------------------------------- lookups
    def resolve_id(self, short: str, kinds: tuple[str, ...] | None = None) -> str:
        """Expand a short id; refuse ambiguity (SPEC §6.1).

        Measured (50k-object catalog): a plain ``id LIKE 'prefix%'`` never
        used the covering index on ``id`` here — SQLite's LIKE-to-range
        rewrite only fires when the pattern is unaffected by ASCII case
        folding, and our ids are lowercase hex (a-f *are* case-folded), so
        it fell back to a full index scan (~3.2 ms/lookup). All ids are
        exactly 64 lowercase-hex characters, so a hex-only short prefix can
        be turned into an explicit ``id >= lo AND id < hi`` range — provably
        equivalent to the LIKE for this alphabet (see
        tests/test_store_perf.py) and index-seekable (~6 µs/lookup, ~500x).
        Non-hex input (never a real id, but tolerated rather than rejected)
        falls back to the original LIKE scan unchanged.

        The optional ``kind`` filter is applied in Python after the id
        lookup rather than as an SQL ``AND kind IN (...)``: with the filter
        in SQL, the planner preferred the secondary ``objects_kind`` index
        over the id range/index, undoing the win above. A prefix match is
        always a handful of rows at most, so the Python-side filter is free.

        (Re-measured when ``objects_kind`` was replaced by the composite
        ``objects_kind_recent``: the planner now stays on the id index even
        with the kind filter written in SQL. The Python-side filter is kept
        anyway — it costs nothing on a handful of rows and does not depend
        on which secondary index happens to exist or on planner statistics.)
        """
        short = short.removeprefix("sha256:").lower()
        if len(short) == 64:
            return short
        if len(short) < 6:
            raise StoreError(f"id prefix too short: {short!r} (need ≥6 hex chars)")
        if all(c in _HEX_DIGITS for c in short):
            hi = short[:-1] + chr(ord(short[-1]) + 1)
            rows = self.db.execute(
                "SELECT id, kind FROM objects WHERE id >= ? AND id < ? ORDER BY id",
                (short, hi),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, kind FROM objects WHERE id LIKE ? ORDER BY id", (short + "%",)
            ).fetchall()
        if kinds:
            rows = [r for r in rows if r[1] in kinds]
        ids = [r[0] for r in rows]
        if not ids:
            raise UnknownIdError(
                f"no object matches id prefix {short!r} in this workspace; it was "
                "either never captured here, or `ctx gc` / the retention window "
                "has already collected it. Re-capture the evidence "
                "(`ctx run -- <command>`); `ctx pin <handle>` keeps an artifact "
                "past retention next time"
            )
        if len(ids) > 1:
            raise AmbiguousIdError(short, ids)
        return ids[0]

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

    def gc(self, retention_days: int, *, override_retention: bool = False) -> dict[str, int]:
        """Mark-and-sweep over leases and pins. Blobs referenced by any live
        manifest survive. Never touches objects with a pin.

        ``override_retention`` marks a horizon the USER supplied explicitly
        (``ctx gc --retention-days N``) rather than the workspace default.
        It exists because those are different claims:

        * By default a ``retention`` lease -- minted at write time from the
          configured policy -- protects its object even past the recency
          cutoff. That is deliberate and pinned by
          tests/test_pr1_review_fixes.py.
        * But it made the FLAG advisory. `ctx gc --retention-days 0`, which
          admin.py documents as "collect everything already expired", left
          every freshly-written manifest alive behind a 30-day lease it had
          minted for itself moments earlier -- the user's explicit horizon
          losing to the default it was written to override.

        So an explicit horizon overrides retention leases and an implicit one
        does not. Pins (NULL expiry) and checkpoint leases are never
        overridden either way: those are protection someone asked for, not a
        default policy being retuned.
        """
        now = time.time()
        cutoff = now - retention_days * 86400
        live: set[str] = set()
        # Every unexpired lease keeps its object alive — pins (NULL expiry)
        # and time-bounded retention/checkpoint leases alike. Expired leases
        # no longer protect anything.
        # `retention` leases are EXCLUDED here: they were minted at write
        # time from the Store's configured retention_days, so honouring them
        # made this argument advisory. `ctx gc --retention-days 0` -- which
        # admin.py documents as "collect everything already expired" -- left
        # every freshly-written manifest alive behind a 30-day lease it had
        # minted for itself moments earlier. The horizon this call was GIVEN
        # is the horizon it uses, and `recent` below applies it uniformly.
        #
        # Pins (NULL expiry) and checkpoint leases are untouched: those are
        # explicit protection someone asked for, not a default policy the
        # caller is overriding.
        lease_sql = "SELECT id FROM leases WHERE (expires_at IS NULL OR expires_at > ?)"
        if override_retention:
            lease_sql += " AND reason != 'retention'"
        leased = {r[0] for r in self.db.execute(lease_sql, (now,))}
        recent = {
            r[0]
            for r in self.db.execute("SELECT id FROM objects WHERE created_at >= ?", (cutoff,))
        }
        live |= leased | recent
        # Mark blobs referenced by live manifests. Discovery is STRUCTURAL:
        # every sha256-shaped string anywhere in the document counts as a
        # reference. This used to read exactly two hand-maintained places --
        # manifest["streams"][*]["blob"] and manifest["blob"] -- so any
        # manifest kind that carried its blobs elsewhere was invisible to the
        # mark phase. A bug bash confirmed the consequence: gc() deleted blobs
        # still referenced by a LIVE ctx.investigation/v1 manifest, which is
        # data loss, and the field list would have had to grow by hand for
        # every future kind. Over-marking is the safe direction for a
        # collector -- an extra live id merely survives a cycle.
        for mid in list(live):
            mpath = self.manifest_dir / f"{mid}.json"
            if mpath.is_file():
                try:
                    manifest = json.loads(mpath.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                live |= _referenced_blobs(manifest)
        removed_blobs = removed_manifests = 0
        # Files are unlinked eagerly per object (unchanged); the two catalog
        # DELETEs are batched into one transaction with executemany instead
        # of a `with self.db:` commit per dead object — measured 3.7x on a
        # 50k-object catalog with ~25k dead (3973 ms -> 1061 ms), since each
        # commit was paying its own transaction/WAL overhead. End state
        # (surviving vs removed objects, return value) is unchanged; only
        # the number of commits shrinks.
        dead_ids: list[str] = []
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
            dead_ids.append(obj_id)
        if dead_ids:
            params = [(i,) for i in dead_ids]
            with self.db:
                self.db.executemany("DELETE FROM objects WHERE id=?", params)
                self.db.executemany("DELETE FROM leases WHERE id=?", params)
        return {"blobs_removed": removed_blobs, "manifests_removed": removed_manifests}

    # --------------------------------------------------------------- spans
    @staticmethod
    def span_id_for(blob_hash: str, kind: str, *params: object) -> str:
        """Deterministic span identity: a pure function of the artifact bytes'
        hash plus the span parameters — replayable across sessions, machines,
        and re-digests (unlike a TTL'd cache token)."""
        blob_hash = blob_hash.removeprefix("sha256:")
        seed = "|".join([blob_hash, kind, *(str(p) for p in params)])
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]

    def register_span(
        self,
        blob_hash: str,
        kind: str,
        *,
        a: int | None = None,
        b: int | None = None,
        template: str | None = None,
        note: str = "",
    ) -> str:
        blob_hash = blob_hash.removeprefix("sha256:")
        params = (a, b) if kind == "region" else (template,)
        sid = self.span_id_for(blob_hash, kind, *params)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO spans (span_id, blob, kind, a, b, template, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, blob_hash, kind, a, b, template, note),
            )
        return sid

    def get_span(self, span_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT span_id, blob, kind, a, b, template, note FROM spans WHERE span_id = ?",
            (span_id.strip(),),
        ).fetchone()
        if row is None:
            raise UnknownIdError(
                f"unknown span {span_id!r} in this workspace; span tokens come "
                "from digests — re-run the digest or use --lines coordinates"
            )
        return {
            "span_id": row[0], "blob": row[1], "kind": row[2],
            "a": row[3], "b": row[4], "template": row[5], "note": row[6],
        }

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
