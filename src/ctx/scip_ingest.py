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


def find_index(ws: Workspace) -> Path | None:
    """The workspace's SCIP index, or None. ``$CTX_SCIP_INDEX`` overrides
    (absolute, or relative to the workspace root)."""
    override = os.environ.get("CTX_SCIP_INDEX")
    if override:
        p = Path(override)
        p = p if p.is_absolute() else ws.root / p
        return p if p.is_file() else None
    p = ws.root / _INDEX_NAME
    return p if p.is_file() else None


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


def iter_occurrences(index_path: Path):
    """Yield every :class:`Occurrence` in a SCIP index. Fail-open: an
    unreadable/absent runtime yields nothing (the caller degrades)."""
    pb2 = _scip_pb2()
    if pb2 is None:
        return
    try:
        idx = pb2.Index()
        idx.ParseFromString(Path(index_path).read_bytes())
    except Exception:
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


def refs(ws: Workspace, symbol: str, *, definitions_only: bool = False):
    """Precise reference sites for ``symbol`` from the workspace's SCIP
    index, matching the codeverbs contract: ``list[(rel, line, text)]``
    sorted (file, line). ``text`` is the source line (read from the
    worktree). Returns None when no index is present or the runtime is
    absent — the signal to fall through the engine ladder."""
    index = find_index(ws)
    if index is None or not available():
        return None
    want = symbol.rsplit(".", 1)[-1]  # dotted subject → its final component
    hits: dict[tuple[str, int], str] = {}
    line_cache: dict[str, list[str]] = {}
    for occ in iter_occurrences(index):
        if occ.name != want:
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
        # An index exists but names nothing — still a definitive SCIP answer
        # for this symbol (empty), distinct from "no index" (None).
        return []
    return [(f, ln, hits[(f, ln)]) for (f, ln) in sorted(hits)]
