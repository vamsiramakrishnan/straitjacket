"""A persistent, incremental text index for the workspace: trigrams, symbols,
and per-file fingerprints, Zoekt-shaped and staleness-proof by construction.

Why it exists
-------------
Every retrieval verb that searched the repository re-read every file on every
call (`ctx search`, the `q` search stage, `ctx pack`'s ranking). That is fine
for a grep and useless for the questions a context engine needs answered
cheaply and repeatedly: *which files mention this term at all*, *how rare is
this term across the corpus*, *which files define a symbol by this name*.
Those are index questions, and an index that is allowed to go stale is worse
than none — it answers with confidence about a tree that no longer exists.

The staleness contract
----------------------
An index is only ever a *candidate generator*. Three rules keep it honest:

1. **Fingerprint sweep before every query.** ``Index.sync`` stats every
   eligible file (6 ms for 870 files, ~250 ms for 50k) and re-indexes the ones
   whose ``(size, mtime_ns)`` moved and whose content hash actually differs.
   Deleted files leave the catalog in the same pass. So at the moment a query
   runs, the catalog describes the tree on disk.
2. **Verification against live bytes.** Candidates are files the index says
   *may* match. The caller reads the current file and runs the real pattern;
   a stale posting can produce a wasted read, never a wrong answer.
3. **Superset by design.** Trigrams are taken over the lowercased bytes, so a
   case-sensitive pattern's candidates are a superset of its matches; a
   regex contributes only the literal runs it *must* contain (Cox 2012),
   and a pattern with no run of three literal bytes is answered by a full
   scan, declared as such.

Shape
-----
Segments are immutable files (``seg-NNNNNN.tri``): a JSON header with the
file table (path, sha, size, mtime, language, line count, symbols), a sorted
array of 24-bit trigram keys, an offsets array, and delta-varint postings of
local file ids. A changed file is appended to a *new* segment and the catalog
repointed; the old posting is dead and ignored because the catalog no longer
names its ``(segment, id)``. When segments pile up the index compacts by
re-reading the live files. Nothing here needs a daemon, a watcher or a
third-party package; it lives in the store's ``indexes/`` area, never in the
worktree.

Symbols ride along because the same sweep that reads a changed file can
outline it (tree-sitter/ctags/ast, via :mod:`ctx.skeleton`); ``sym:`` filters
and :mod:`ctx.pack`'s symbol signal then cost a dictionary lookup instead of
a repository parse.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import struct
import time
from array import array
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ctx.sessiondir import LEDGER_DIR_NAME
from ctx.store import Store, _atomic_write
from ctx.workspace import Workspace

INDEX_VERSION = 1
_MAGIC = b"CTXTRI1\n"
#: Files larger than this are not indexed (declared in ``status()``); a
#: minified bundle or a data dump has trigrams of everything and tells nothing.
MAX_FILE_BYTES = 1 << 20
#: Files per segment on a full build; bounds the build's working memory.
SEGMENT_FILES = 1000
#: Compact when this many segments accumulate.
MAX_SEGMENTS = 16
#: Above this the first build is not done implicitly inside a retrieval verb
#: (a hook's latency budget); ``ctx index --text`` builds it explicitly.
IMPLICIT_BUILD_MAX_FILES = 4000
IMPLICIT_BUILD_MAX_BYTES = 48 << 20

_TRI_RE = re.compile(rb"(?s)...")


# ----------------------------------------------------------------- trigrams
def trigram_keys(data: bytes) -> set[int]:
    """The set of 24-bit trigram keys of ``data`` lowercased.

    Three stride-3 C-speed scans cover every offset; the union is the set of
    all overlapping trigrams. Measured: 11.5 MB of source in 2.8 s including
    the reads, versus 30+ s for a Python slice loop.
    """
    s = data.lower()
    if len(s) < 3:
        return set()
    grams = set(_TRI_RE.findall(s))
    grams.update(_TRI_RE.findall(s[1:]))
    grams.update(_TRI_RE.findall(s[2:]))
    return {int.from_bytes(g, "big") for g in grams}


def _literal_keys(text: str) -> set[int]:
    return trigram_keys(text.encode("utf-8", "surrogateescape"))


# ------------------------------------------------------ trigram expressions
# An expression is True (unrestricted: every file is a candidate) or a tuple:
#   ("tri", frozenset[int])       every key must be present
#   ("and", [expr, ...])          all sub-expressions
#   ("or", [expr, ...])           any sub-expression
Expr = Any


def expr_for_literal(text: str) -> Expr:
    keys = _literal_keys(text)
    return ("tri", frozenset(keys)) if keys else True


def _and(parts: list[Expr]) -> Expr:
    parts = [p for p in parts if p is not True]
    if not parts:
        return True
    return parts[0] if len(parts) == 1 else ("and", parts)


def _or(parts: list[Expr]) -> Expr:
    if any(p is True for p in parts) or not parts:
        return True
    return parts[0] if len(parts) == 1 else ("or", parts)


def expr_for_regex(pattern: str, *, fixed: bool = False) -> Expr:
    """The trigrams a match of ``pattern`` must contain (a conservative
    subset), from the compiled parse tree: literal runs inside groups and
    required repeats, OR across alternation, breakers everywhere else."""
    if fixed:
        return expr_for_literal(pattern)
    try:
        import re._parser as sre  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - 3.11+ always has it
        import sre_parse as sre  # type: ignore[no-redef]
    try:
        tree = sre.parse(pattern)
    except Exception:
        return True
    return _expr_for_subpattern(tree, sre)


def _expr_for_subpattern(sub, sre) -> Expr:
    parts: list[Expr] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 3:
            parts.append(expr_for_literal("".join(run)))
        run.clear()

    for op, av in sub:
        if op is sre.LITERAL:
            run.append(chr(av))
            continue
        flush()
        if op is sre.SUBPATTERN:
            inner = av[-1]
            parts.append(_expr_for_subpattern(inner, sre))
        elif op in (sre.MAX_REPEAT, sre.MIN_REPEAT) or getattr(sre, "POSSESSIVE_REPEAT", None) is op:
            lo, _hi, inner = av
            if lo >= 1:
                parts.append(_expr_for_subpattern(inner, sre))
        elif op is sre.BRANCH:
            _, branches = av
            parts.append(_or([_expr_for_subpattern(b, sre) for b in branches]))
        # everything else (ANY, IN, AT, CATEGORY, NOT_LITERAL, GROUPREF,
        # ASSERT...) is a breaker that requires nothing
    flush()
    return _and(parts)


def is_unrestricted(expr: Expr) -> bool:
    return expr is True


# ---------------------------------------------------------------- postings
def _encode_postings(ids: list[int]) -> bytes:
    out = bytearray()
    prev = -1
    for i in ids:
        d = i - prev
        prev = i
        while d >= 0x80:
            out.append((d & 0x7F) | 0x80)
            d >>= 7
        out.append(d)
    return bytes(out)


def _decode_postings(buf, start: int, end: int) -> list[int]:
    out: list[int] = []
    prev = -1
    i = start
    while i < end:
        shift = 0
        val = 0
        while True:
            b = buf[i]
            i += 1
            val |= (b & 0x7F) << shift
            if b < 0x80:
                break
            shift += 7
        prev += val
        out.append(prev)
    return out


# ----------------------------------------------------------------- segments
@dataclass(slots=True)
class FileMeta:
    rel: str
    sha: str
    size: int
    mtime_ns: int
    lang: str | None
    lines: int
    symbols: list[list[Any]]  # [name, kind, line]


class Segment:
    """One immutable segment, memory-mapped on open."""

    def __init__(self, path: Path):
        self.path = path
        self.id = path.stem
        with path.open("rb") as fh:
            magic = fh.read(len(_MAGIC))
            if magic != _MAGIC:
                raise ValueError(f"not a segment: {path}")
            (hlen,) = struct.unpack("<I", fh.read(4))
            self.header = json.loads(fh.read(hlen).decode("utf-8"))
            self.files: list[dict[str, Any]] = self.header["files"]
            ntri = int(self.header["ntri"])
            keys = array("I")
            keys.frombytes(fh.read(ntri * 4))
            offs = array("I")
            offs.frombytes(fh.read((ntri + 1) * 4))
            self._keys = keys
            self._offs = offs
            self._base = fh.tell()
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
        self._size = size
        self._fh = path.open("rb")
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ) if size else b""

    def close(self) -> None:
        try:
            if isinstance(self._mm, mmap.mmap):
                self._mm.close()
            self._fh.close()
        except Exception:
            pass

    def postings(self, key: int) -> list[int]:
        keys = self._keys
        i = bisect_left(keys, key)
        if i >= len(keys) or keys[i] != key:
            return []
        a = self._base + self._offs[i]
        b = self._base + self._offs[i + 1]
        return _decode_postings(self._mm, a, b)

    def candidates(self, expr: Expr) -> set[int] | None:
        """Local ids matching ``expr``; None means every file (unrestricted)."""
        if expr is True:
            return None
        kind = expr[0]
        if kind == "tri":
            keys = sorted(expr[1])
            result: set[int] | None = None
            for k in keys:
                ids = set(self.postings(k))
                result = ids if result is None else result & ids
                if not result:
                    return set()
            return result if result is not None else None
        if kind == "and":
            result = None
            for part in expr[1]:
                got = self.candidates(part)
                if got is None:
                    continue
                result = got if result is None else result & got
                if not result:
                    return set()
            return result
        if kind == "or":
            acc: set[int] = set()
            for part in expr[1]:
                got = self.candidates(part)
                if got is None:
                    return None
                acc |= got
            return acc
        raise ValueError(f"bad expr {kind!r}")

    @staticmethod
    def write(path: Path, entries: list[tuple[FileMeta, set[int]]]) -> None:
        postings: dict[int, array] = {}
        for lid, (_meta, keys) in enumerate(entries):
            for k in keys:
                arr = postings.get(k)
                if arr is None:
                    arr = postings[k] = array("I")
                arr.append(lid)
        keys_sorted = sorted(postings)
        offs = array("I")
        body = bytearray()
        for k in keys_sorted:
            offs.append(len(body))
            body += _encode_postings(list(postings[k]))
        offs.append(len(body))
        header = {
            "version": INDEX_VERSION,
            "files": [
                {
                    "rel": m.rel, "sha": m.sha, "size": m.size, "mtime_ns": m.mtime_ns,
                    "lang": m.lang, "lines": m.lines, "symbols": m.symbols,
                }
                for m, _ in entries
            ],
            "ntri": len(keys_sorted),
        }
        hbytes = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        karr = array("I", keys_sorted)
        blob = (
            _MAGIC + struct.pack("<I", len(hbytes)) + hbytes
            + karr.tobytes() + offs.tobytes() + bytes(body)
        )
        _atomic_write(path, blob)


# ------------------------------------------------------------------ catalog
def index_dir(store: Store) -> Path:
    return store.root / "indexes" / "trigram"


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _eligible(rel: str) -> bool:
    top = rel.replace("\\", "/").split("/")[0]
    return top != LEDGER_DIR_NAME and top != ".git"


def _symbols_for(source: str, lang: str | None, rel: str) -> list[list[Any]]:
    """[name, kind, line] per symbol, via the skeleton backends; fail-open."""
    if lang is None:
        return []
    try:
        from ctx.skeleton import _extract

        syms, _imports, _parser = _extract(source, lang, rel)
    except Exception:
        return []
    out: list[list[Any]] = []
    for s in syms:
        try:
            out.append([str(s["name"]), str(s.get("kind") or ""), int(s["range"][0])])
        except Exception:
            continue
    return out


class Index:
    """The workspace's text index: catalog + segments, synced on demand."""

    def __init__(self, store: Store, ws: Workspace):
        self.store = store
        self.ws = ws
        self.dir = index_dir(store)
        self.catalog_path = self.dir / "catalog.json"
        self.catalog: dict[str, Any] = self._load_catalog()
        self._segments: dict[str, Segment] = {}
        self.last_sync: dict[str, Any] = {}

    # ------------------------------------------------------------- state
    def _load_catalog(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            if int(doc.get("version", 0)) == INDEX_VERSION:
                return doc
        except (OSError, ValueError):
            pass
        return {"version": INDEX_VERSION, "segments": [], "files": {}, "next": 1, "built_at": None}

    def _save_catalog(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self.catalog_path,
            json.dumps(self.catalog, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )

    @property
    def built(self) -> bool:
        return bool(self.catalog.get("built_at"))

    @property
    def files(self) -> dict[str, list[Any]]:
        """rel → [segment, local id, size, mtime_ns, sha, lang, lines]."""
        return self.catalog["files"]

    def segment(self, seg_id: str) -> Segment:
        seg = self._segments.get(seg_id)
        if seg is None:
            seg = self._segments[seg_id] = Segment(self.dir / f"{seg_id}.tri")
        return seg

    def close(self) -> None:
        for seg in self._segments.values():
            seg.close()
        self._segments.clear()

    # -------------------------------------------------------------- sync
    def _listing(self) -> list[str]:
        return [r for r in self.ws.list_files(None) if _eligible(r)]

    def corpus_estimate(self) -> tuple[int, int]:
        """(files, bytes) the index would cover — stats only."""
        rels = self._listing()
        total = 0
        root = self.ws.root
        for rel in rels:
            try:
                total += (root / rel).stat().st_size
            except OSError:
                continue
        return len(rels), total

    def sync(self, *, force: bool = False) -> dict[str, Any]:
        """Bring the catalog in step with the tree: stat every eligible file,
        re-index the changed ones into a new segment, drop the deleted ones,
        compact when segments pile up. Returns a receipt."""
        t0 = time.monotonic()
        root = self.ws.root
        files = self.files
        rels = self._listing()
        present = set(rels)
        removed = [rel for rel in files if rel not in present]
        for rel in removed:
            del files[rel]

        changed: list[tuple[str, int, int]] = []
        for rel in rels:
            try:
                st = (root / rel).stat()
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES:
                continue
            ent = files.get(rel)
            if ent is None or force or ent[2] != st.st_size or ent[3] != st.st_mtime_ns:
                changed.append((rel, st.st_size, st.st_mtime_ns))

        indexed = 0
        skipped_binary = 0
        entries: list[tuple[FileMeta, set[int]]] = []
        if changed:
            from ctx.skeleton import language_for

            for rel, size, mtime_ns in changed:
                try:
                    data = (root / rel).read_bytes()
                except OSError:
                    continue
                if b"\x00" in data[:8192]:
                    skipped_binary += 1
                    files.pop(rel, None)
                    continue
                sha = _sha12(data)
                ent = files.get(rel)
                if ent is not None and ent[4] == sha and not force:
                    # touched, not changed: refresh the fingerprint only
                    ent[2], ent[3] = size, mtime_ns
                    continue
                lang = language_for(rel)
                text = data.decode("utf-8", "replace")
                meta = FileMeta(
                    rel=rel, sha=sha, size=size, mtime_ns=mtime_ns, lang=lang,
                    lines=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
                    symbols=_symbols_for(text, lang, rel),
                )
                entries.append((meta, trigram_keys(data)))
                indexed += 1
                if len(entries) >= SEGMENT_FILES:
                    self._append_segment(entries)
                    entries = []
            if entries:
                self._append_segment(entries)

        compacted = False
        if len(self.catalog["segments"]) > MAX_SEGMENTS:
            self._compact()
            compacted = True

        if changed or removed or not self.built or compacted:
            self.catalog["built_at"] = self.catalog.get("built_at") or time.time()
            self.catalog["synced_at"] = time.time()
            self._save_catalog()

        self.last_sync = {
            "files": len(files), "checked": len(rels), "indexed": indexed,
            "removed": len(removed), "binary": skipped_binary,
            "segments": len(self.catalog["segments"]), "compacted": compacted,
            "ms": round((time.monotonic() - t0) * 1000, 1),
        }
        return self.last_sync

    def _append_segment(self, entries: list[tuple[FileMeta, set[int]]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        n = int(self.catalog.get("next", 1))
        seg_id = f"seg-{n:06d}"
        self.catalog["next"] = n + 1
        Segment.write(self.dir / f"{seg_id}.tri", entries)
        self.catalog["segments"].append(seg_id)
        for lid, (m, _) in enumerate(entries):
            self.files[m.rel] = [seg_id, lid, m.size, m.mtime_ns, m.sha, m.lang, m.lines]

    def _compact(self) -> None:
        """Rewrite the live files into fresh segments and drop the old ones.
        Re-reads from disk: the cheapest correct merge, and it re-verifies."""
        live = sorted(self.files)
        old = list(self.catalog["segments"])
        self.close()
        self.catalog["segments"] = []
        self.catalog["files"] = {}
        root = self.ws.root
        entries: list[tuple[FileMeta, set[int]]] = []
        from ctx.skeleton import language_for

        for rel in live:
            try:
                st = (root / rel).stat()
                data = (root / rel).read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192] or st.st_size > MAX_FILE_BYTES:
                continue
            lang = language_for(rel)
            text = data.decode("utf-8", "replace")
            meta = FileMeta(
                rel=rel, sha=_sha12(data), size=st.st_size, mtime_ns=st.st_mtime_ns,
                lang=lang, lines=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
                symbols=_symbols_for(text, lang, rel),
            )
            entries.append((meta, trigram_keys(data)))
            if len(entries) >= SEGMENT_FILES:
                self._append_segment(entries)
                entries = []
        if entries:
            self._append_segment(entries)
        for seg_id in old:
            try:
                (self.dir / f"{seg_id}.tri").unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ queries
    def _current(self, seg_id: str, lid: int, rel: str) -> bool:
        ent = self.files.get(rel)
        return ent is not None and ent[0] == seg_id and ent[1] == lid

    def candidates(self, expr: Expr, *, subset: Iterable[str] | None = None) -> list[str]:
        """Repo-relative paths that may match ``expr`` (sorted). ``subset``
        restricts the answer to those paths (a glob or scope pre-filter)."""
        allowed = set(subset) if subset is not None else None
        if expr is True:
            rels = list(self.files) if allowed is None else [r for r in self.files if r in allowed]
            return sorted(rels)
        out: list[str] = []
        for seg_id in self.catalog["segments"]:
            try:
                seg = self.segment(seg_id)
            except (OSError, ValueError):
                continue
            ids = seg.candidates(expr)
            if ids is None:
                ids = set(range(len(seg.files)))
            for lid in ids:
                rel = seg.files[lid]["rel"]
                if allowed is not None and rel not in allowed:
                    continue
                if self._current(seg_id, lid, rel):
                    out.append(rel)
        return sorted(out)

    def df(self, text: str) -> int:
        """Files that may contain ``text`` (trigram candidates; exact for
        terms whose trigrams are specific enough, an upper bound otherwise)."""
        return len(self.candidates(expr_for_literal(text)))

    def meta(self, rel: str) -> dict[str, Any] | None:
        ent = self.files.get(rel)
        if ent is None:
            return None
        try:
            seg = self.segment(ent[0])
        except (OSError, ValueError):
            return None
        return seg.files[ent[1]]

    def symbols(self, rel: str) -> list[list[Any]]:
        m = self.meta(rel)
        return list(m.get("symbols") or []) if m else []

    def files_defining(self, name: str, *, exact: bool = True) -> list[tuple[str, str, int]]:
        """(rel, kind, line) for every indexed symbol named ``name`` (or whose
        name contains it, case-insensitively, when ``exact`` is False)."""
        want = name.lower()
        out: list[tuple[str, str, int]] = []
        for seg_id in self.catalog["segments"]:
            try:
                seg = self.segment(seg_id)
            except (OSError, ValueError):
                continue
            for lid, f in enumerate(seg.files):
                syms = f.get("symbols") or []
                if not syms or not self._current(seg_id, lid, f["rel"]):
                    continue
                for sname, kind, line in syms:
                    s = str(sname)
                    if (s == name) if exact else (want in s.lower()):
                        out.append((f["rel"], str(kind), int(line)))
        out.sort()
        return out

    def status(self) -> dict[str, Any]:
        segs = self.catalog["segments"]
        size = 0
        for seg_id in segs:
            try:
                size += (self.dir / f"{seg_id}.tri").stat().st_size
            except OSError:
                pass
        by_lang: dict[str, int] = {}
        for ent in self.files.values():
            by_lang[ent[5] or "text"] = by_lang.get(ent[5] or "text", 0) + 1
        return {
            "built": self.built, "files": len(self.files), "segments": len(segs),
            "bytes": size, "dir": str(self.dir), "languages": by_lang,
            "max_file_bytes": MAX_FILE_BYTES, "synced_at": self.catalog.get("synced_at"),
        }


# ----------------------------------------------------------------- opening
def enabled() -> bool:
    return os.environ.get("CTX_SEARCH_INDEX", "").lower() not in ("off", "0", "false")


def open_index(store: Store, ws: Workspace, *, build: bool = False) -> Index | None:
    """The workspace index, synced, or None when it is off or not yet built
    and too large to build implicitly (``ctx index --text`` builds it). With
    ``build`` the size guard is waived."""
    if not enabled():
        return None
    idx = Index(store, ws)
    if not idx.built and not build:
        n, nbytes = idx.corpus_estimate()
        if n > IMPLICIT_BUILD_MAX_FILES or nbytes > IMPLICIT_BUILD_MAX_BYTES:
            return None
    idx.sync()
    return idx
