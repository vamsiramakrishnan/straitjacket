"""Shared errors, ref parsing, and emission helpers for the retrieval package.

Internal — nothing outside ``ctx.retrieval`` (the public facade) and its
sibling ``ctx._retrieval.*`` modules should import from here directly.
"""

from __future__ import annotations

import re

from ctx.refs import Ref, parse_ref
from ctx.store import Store
from ctx.textutil import _redaction_of, bounded, redact, sanitize_for_model
from ctx.workspace import Workspace


class RetrievalError(Exception):
    pass


def _emit(
    ws: Workspace,
    text: str,
    budget_tokens: int,
    continuation: str | None = None,
    *,
    handle: str | None = None,
    exact: bool = False,
) -> str:
    """Bound a retrieval result, keeping a way back to the rest of it.

    ``continuation`` is set when the *caller* decided to cut (a line span it
    clipped itself). ``handle`` covers the other cut: ``bounded`` trimming on
    the token budget, which the caller cannot predict. Without it a
    budget-truncated result ends at the bare truncation note, with no address
    — the same defect fixed on the digest side, and it lives here too.

    ``exact`` marks an EXACT-BYTES answer (``ctx get --bytes``). Control
    stripping is a display nicety everywhere else and the thing that makes
    this answer wrong: it silently deletes every byte below 0x20 from a slice
    the caller asked for verbatim. Redaction still runs -- it is a security
    control, not a presentation one, and it announces itself when it fires,
    so the one case where an exact answer is not byte-exact says so.
    """
    if exact:
        # The exact path skips control STRIPPING, not redaction -- but it goes
        # through the same switch, so `[redaction] enabled = false` means the
        # same thing here as everywhere else.
        enabled, patterns = _redaction_of(ws.config.redaction)
        text, redactions = redact(text, patterns) if enabled else (text, [])
    else:
        text, redactions = sanitize_for_model(text, ws.config.redaction)
    if redactions:
        text += "\nredaction: applied [" + ", ".join(redactions) + "]"
    fallback = f"ctx get {handle}" if handle else None
    return bounded(text, budget_tokens, continuation, truncation_continuation=fallback)


def _parse(ref_text: str) -> Ref:
    return parse_ref(ref_text)


def _peek_blob(store: Store, blob_hash: str, n: int = 8192) -> bytes:
    """Read only the first ``n`` bytes of a blob (default: the same 8 KiB
    binary-sniff window used everywhere else in this package).

    Perf/memory (task 3, "bounded memory on huge blobs"): a blob that turns
    out to be binary must not be fully materialized into a Python ``bytes``
    object just to be rejected. Callers that need the binary verdict before
    deciding whether to do a full ``store.get_blob`` read use this instead.
    """
    from ctx.store import MIN_ID_DISPLAY, UnknownIdError

    resolved = store.resolve_id(blob_hash, kinds=("blob",))
    try:
        with store.blob_path(resolved).open("rb") as fh:
            return fh.read(n)
    except FileNotFoundError:
        # Parity with Store.get_blob's own not-found translation.
        raise UnknownIdError(f"blob:{resolved[:MIN_ID_DISPLAY]} not found") from None


def _read_bytes_range(store: Store, blob_hash: str, a: int, b: int) -> bytes:
    """Byte range [a, b] (1-indexed, inclusive) without loading the whole
    blob into memory — mirrors ``data[a - 1 : b]`` slicing semantics
    exactly (short reads near EOF silently return fewer bytes, never an
    error), just backed by a seek instead of a full read.

    Perf/memory (task 3): the ``--bytes`` selector is, by construction, a
    request for a small bounded slice; loading a 50 MiB blob to return 40
    requested bytes was the unbounded-memory gap this closes.
    """
    resolved = store.resolve_id(blob_hash, kinds=("blob",))
    path = store.blob_path(resolved)
    start = max(0, a - 1)
    length = b - start
    if length <= 0:
        return b""
    with path.open("rb") as fh:
        fh.seek(start)
        return fh.read(length)


def _route_workspace(store: Store, ws: Workspace, ref: Ref) -> tuple[Store, Workspace]:
    """Resolve a ws:<alias>/ reference to its target workspace and store.

    A reference carrying an alias must never silently execute against the
    current workspace (wrong-repository evidence). Unknown aliases are
    rejected with the fix, not guessed (SPEC §5.1, §15).
    """
    if ref.workspace_alias is None:
        return store, ws
    from pathlib import Path as _Path

    from ctx.workspace import resolve_workspace

    path = ws.config.aliases.get(ref.workspace_alias)
    if path is None:
        known = ", ".join(sorted(ws.config.aliases)) or "none configured"
        raise RetrievalError(
            f"unknown workspace alias {ref.workspace_alias!r} (known: {known}); "
            "define it under [aliases] in ctx.toml or pass --workspace"
        )
    target_path = _Path(path)
    if not target_path.is_absolute():
        target_path = ws.root / target_path
    target = resolve_workspace(str(target_path))
    return Store(target.workspace_id), target


# Hoisted (perf): compiled once at import time rather than per ``_span()``
# call. ``re.match``/``re.compile`` on a repeated literal pattern is already
# memoized by the stdlib's internal cache, but that cache is shared and can
# be evicted by the arbitrary user-supplied search patterns compiled
# elsewhere in this package (``ctx.retrieval.search``) — a module-level
# compile guarantees this one never pays for that churn. Measured in the
# perf pass; see the report for before/after numbers.
_SPAN_RE = re.compile(r"^(\d+):(\d+)$")


def _span(spec: str) -> tuple[int, int]:
    m = _SPAN_RE.match(spec.strip())
    if not m:
        raise RetrievalError(f"invalid span {spec!r}; expected A:B")
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        raise RetrievalError(f"invalid span {spec!r}: need 1 <= A <= B")
    return a, b
