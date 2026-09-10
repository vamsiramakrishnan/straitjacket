"""M-K4 · SCIP ingestion (docs/SUBSTRATE.md §M-K4, ALGEBRA.md M-G).

Opportunistic, precise cross-references. When a workspace carries a SCIP
index (``index.scip`` at the root, or ``$CTX_SCIP_INDEX``) — the protobuf
emitted by a single-binary indexer like ``scip-python`` / ``scip-typescript``
/ ``scip-java`` — this reads it into reference sites with a labeled
precision tier (``scip``: compiler/type-backed, exact). It sits at the top
of the ``refs`` engine ladder above jedi and the ast approximation.

The ripgrep pattern, applied to a library: the protobuf runtime is the
``[scip]`` extra; the generated bindings are vendored
(``ctx._vendor.scip_pb2``). Absence of either costs nothing — every entry
point probes and degrades to None, never raises. The index is never
generated here (indexing is a separate build step); it is only *read* when
present, exactly like SCIP/LSIF ingestion was specified.

SCIP symbol strings look like::

    scip-python python scipproj 0.0.1 `pkg.core`/helper().

The local identifier is the last identifier token in the string
(``helper``); occurrence ranges are 0-indexed ``[line, startCol, endCol]``
(same line) or ``[startLine, startCol, endLine, endCol]``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ctx.workspace import Workspace

_INDEX_NAME = "index.scip"
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFINITION_ROLE = 0x1  # SymbolRole.Definition bit


def _scip_pb2():
    """The vendored bindings, or None when the protobuf runtime ([scip]
    extra) is not importable."""
    try:
        from ctx._vendor import scip_pb2  # requires the protobuf runtime

        return scip_pb2
    except Exception:
        return None


def available() -> bool:
    """True when SCIP indexes can be parsed here (protobuf importable)."""
    return _scip_pb2() is not None


#: Sidecar `ctx index` writes beside a generated index, recording the tree it
#: describes. Absent for an index a project built itself, which is why the
#: currency check degrades to the index file's own mtime.
_SIDECAR_NAME = "index.meta.json"


def _source_state(ws: Workspace) -> tuple[int, int]:
    """(file count, newest mtime_ns) over the workspace's *source* files.

    Restricted to files a code indexer would read, decided by the skeleton's
    own language table so this cannot disagree with the rest of ctx about what
    source is. A touched README does not invalidate a code index, and neither
    does the index — or its own sidecar — sitting in the worktree.

    Stats only, no reads: this runs on the retrieval path, whereas the heavier
    :func:`ctx.workspace.stat_fingerprint` hashes bytes because a rewrite
    guard needs evidence a timestamp cannot give.
    """
    from ctx.skeleton import language_for

    newest = 0
    count = 0
    root = ws.root
    for rel in ws.list_files(None):
        if language_for(rel) is None:
            continue
        count += 1
        try:
            mtime = (root / rel).stat().st_mtime_ns
        except OSError:
            continue
        if mtime > newest:
            newest = mtime
    return count, newest


def _index_basis(index: Path) -> int | None:
    """The moment ``index`` describes, in mtime_ns, or None if unreadable.

    The sidecar's recorded high-water mark beats the index file's own mtime
    where both exist, because an indexer may finish writing well after it read
    the last source file.
    """
    try:
        basis = index.stat().st_mtime_ns
    except OSError:
        return None
    try:
        meta = json.loads(index.with_name(_SIDECAR_NAME).read_text(encoding="utf-8"))
        basis = max(basis, int(meta["max_mtime_ns"]))
    except Exception:
        pass
    return basis


def index_is_current(ws: Workspace, index: Path) -> bool:
    """Whether ``index`` still describes the worktree.

    A SCIP index is a snapshot of a tree at one moment, and nothing keeps it in
    step with edits afterwards. Trusting a stale one is worse than having none:
    the coordinates it returns are exact-looking and wrong, and — the reason
    this exists — an *empty* answer from a stale index would suppress the rest
    of the ladder, so `ctx refs` would confidently report `sites: 0` for a
    symbol added since indexing. A clean wrong answer beats a noisy right one
    nowhere, least of all here.

    Basis: no tracked file may be newer than the index, and (when `ctx index`
    left its sidecar) the file count must match. The bound worth stating is
    that a deletion which touches no surviving file is invisible to a
    mtime-only check — that is why the sidecar records the count at all.
    """
    basis = _index_basis(index)
    if basis is None:
        return False
    recorded_count: int | None = None
    try:
        meta = json.loads(index.with_name(_SIDECAR_NAME).read_text(encoding="utf-8"))
        recorded_count = int(meta["files"])
    except Exception:
        pass  # a project's own index has no sidecar; mtime alone still bounds it

    count, newest = _source_state(ws)
    if newest > basis:
        return False
    return recorded_count is None or recorded_count == count


def find_index(ws: Workspace, store=None) -> Path | None:
    """The workspace's SCIP index, or None. ``$CTX_SCIP_INDEX`` overrides
    (absolute, or relative to the workspace root).

    Three places, in order: the override, an ``index.scip`` a build already
    put in the worktree, then the one ``ctx index`` generated into the store.
    The store copy is last so a project that indexes itself keeps priority
    over ctx's, and first-class enough that ctx never has to write a build
    artifact into someone's repository to make the precise tier reachable.
    """
    override = os.environ.get("CTX_SCIP_INDEX")
    if override:
        p = Path(override)
        p = p if p.is_absolute() else ws.root / p
        return p if p.is_file() else None
    p = ws.root / _INDEX_NAME
    if p.is_file():
        return p
    try:
        from ctx.scip_index import index_path

        if store is not None:
            generated = index_path(store)
        else:
            # Only when the caller has none: every retrieval verb already
            # holds an open store, and opening a second one per lookup is
            # both wasted work and an avoidable lock on the hot path.
            from ctx.store import Store

            own = Store(ws.workspace_id)
            try:
                generated = index_path(own)
            finally:
                own.close()
        return generated if generated.is_file() else None
    except Exception:
        return None  # a store that will not open is not an indexing error


#: SCIP's local-symbol convention: `local <id>`. The word "local" matches
#: the identifier regex, so a local symbol returned the literal name
#: "local" -- a plausible-looking descriptor for something the docstring
#: promises is None.
_SCIP_LOCAL_RE = re.compile(r"^local\s")


def descriptor_name(scip_symbol: str) -> str | None:
    """The local identifier a SCIP symbol names — the last identifier token
    in the whole symbol string (robust across the scheme/package/descriptor
    grammar; the package name and version precede the descriptors, so the
    final token is always the symbol's own name). ``None`` for a
    local/anonymous symbol carrying no identifier."""
    sym = scip_symbol or ""
    if _SCIP_LOCAL_RE.match(sym):
        return None  # `local 3` -- the word "local" is the scheme, not a name
    toks = _IDENT_RE.findall(sym)
    return toks[-1] if toks else None


@dataclass(frozen=True, slots=True)
class Occurrence:
    file: str  # workspace-relative posix path
    line: int  # 1-indexed
    col_a: int  # 1-indexed start column
    col_b: int  # 1-indexed end column
    symbol: str  # the SCIP symbol string
    name: str | None  # extracted local identifier
    is_definition: bool


def _range_1indexed(rng) -> tuple[int, int, int]:
    """(line, col_a, col_b), 1-indexed, from a SCIP occurrence range.
    Handles the 3-element same-line form and the 4-element form (we key on
    the start line for a site)."""
    r = list(rng)
    if len(r) == 3:
        line0, ca, cb = r
    else:  # [startLine, startChar, endLine, endChar]
        line0, ca = r[0], r[1]
        cb = r[3] if r[0] == r[2] else r[1]  # same-line span, else point
    return int(line0) + 1, int(ca) + 1, int(cb) + 1


def load_index(index_path: Path):
    """The parsed SCIP index, or None when it cannot be read.

    Separated from :func:`iter_occurrences` because "parsed fine and names
    nothing" and "could not be parsed" are the same empty stream to a
    generator, and the difference decides whether an empty answer may be
    trusted. A truncated or corrupt index that silently yielded no rows would
    otherwise let `ctx refs` report zero references for *every* symbol, in the
    exact tier's voice, with the rest of the ladder suppressed.
    """
    pb2 = _scip_pb2()
    if pb2 is None:
        return None
    try:
        idx = pb2.Index()
        idx.ParseFromString(Path(index_path).read_bytes())
        return idx
    except Exception:
        return None


def iter_occurrences(index_path: Path):
    """Yield every :class:`Occurrence` in a SCIP index. Fail-open: an
    unreadable/absent runtime yields nothing (the caller degrades)."""
    idx = load_index(index_path)
    if idx is None:
        return
    for doc in idx.documents:
        rel = str(doc.relative_path).replace("\\", "/")
        for occ in doc.occurrences:
            line, ca, cb = _range_1indexed(occ.range)
            yield Occurrence(
                file=rel,
                line=line,
                col_a=ca,
                col_b=cb,
                symbol=occ.symbol,
                name=descriptor_name(occ.symbol),
                is_definition=bool(occ.symbol_roles & _DEFINITION_ROLE),
            )


def refs(ws: Workspace, symbol: str, *, definitions_only: bool = False, store=None):
    """Precise reference sites for ``symbol`` from the workspace's SCIP
    index, matching the codeverbs contract: ``list[(rel, line, text)]``
    sorted (file, line). ``text`` is the source line (read from the
    worktree). Returns None when no *usable* index is present — none at all,
    no protobuf runtime, or one that no longer describes this worktree — which
    is the signal to fall through the engine ladder. An empty list is the
    other thing entirely: a current index that genuinely names no site."""
    index = find_index(ws, store)
    if index is None or not available():
        return None
    if load_index(index) is None:
        # Unreadable is not empty. Same None as "no index": the ladder must
        # keep its lower rungs rather than answer zero for every symbol.
        return None
    if not index_is_current(ws, index):
        # Same signal as "no index", deliberately: the caller's contract is
        # that None means fall through the ladder, and an index that no longer
        # describes this tree has exactly that much to say.
        #
        # Checked up front rather than per-cited-file. A per-file check is
        # cheaper and validates coordinates, but it cannot see a *new* site in
        # a file that changed — so `ctx refs` would answer "scip (exact) ·
        # sites: 52" while silently missing the 53rd. Confident incompleteness
        # is the defect this guard exists to prevent, not a cheaper version of
        # it. The walk measured 220 ms on a 4,700-file worktree, which is real
        # but is dwarfed by the textual scan of every source file that runs
        # instead whenever the answer is that the index cannot be trusted.
        return None
    subject, _, want = symbol.rpartition(".")  # dotted subject → its final component
    qualifier = subject.rsplit(".", 1)[-1] if subject else None
    hits: dict[tuple[str, int], str] = {}
    line_cache: dict[str, list[str]] = {}
    for occ in iter_occurrences(index):
        if occ.name != want:
            continue
        if qualifier is not None:
            # bare-name match alone conflates unrelated symbols sharing a method name; also require the qualifier
            tokens = _IDENT_RE.findall(occ.symbol)
            if len(tokens) < 2 or tokens[-2] != qualifier:
                continue
        if definitions_only and not occ.is_definition:
            continue
        key = (occ.file, occ.line)
        if key in hits:
            continue
        lines = line_cache.get(occ.file)
        if lines is None:
            try:
                lines = (ws.root / occ.file).read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                lines = []
            line_cache[occ.file] = lines
        text = lines[occ.line - 1].strip() if 0 < occ.line <= len(lines) else ""
        hits[key] = text
    if not hits:
        # An index exists, is current, and names nothing — a definitive SCIP
        # answer for this symbol (empty), distinct from "no usable index".
        return []
    return [(f, ln, hits[(f, ln)]) for (f, ln) in sorted(hits)]
