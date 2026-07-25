"""One name, and one join, for the house ledger directory (R2).

``.ctx-session-reads/`` is the workspace-local bookkeeping area: the session
read counter, the reflex/intervention ledgers, the emission-governor state,
the Tier-0 proxy's ``proxy/window.json``, the guard's failure ledger and its
fail-closed ``gate-fallback/`` spill area. It is *bookkeeping, never
evidence* — retrieval, generation hashing and the census walk all exclude it
by name, precisely because it grows as the harness runs and including it
would make the harness observe its own state.

It used to be spelled inline at ~30 places under four different local names
(``_LEDGER_DIR``, ``_LEDGER_DIR_NAME``, ``_GENERATION_EXCLUDE_DIR``,
``_SNAPSHOT_EXCLUDE_DIR``) with two different join styles, and several sites
re-derived a *parent* of a subpath rather than asking for the subpath. None
of the copies disagreed — the cost was that any future change had to land in
all of them.

Hot-path discipline: this module is imported by :mod:`ctx.hook`, which runs
in a fresh interpreter on every intercepted tool call and whose import graph
is pinned by ``tests/test_hook_hot_path.py``. It therefore imports ``os``
only. ``pathlib`` is taken lazily inside :func:`session_reads_path`, the
adapter for the (majority) callers that already hold a ``Path``; the join
itself is defined exactly once, in :func:`session_reads_dir`.
"""

from __future__ import annotations

import os

__all__ = ["LEDGER_DIR_NAME", "session_reads_dir", "session_reads_path"]

#: The directory name itself. Use this when the *name* is what matters —
#: excluding a top-level component from a walk, a glob, or a listing.
LEDGER_DIR_NAME = ".ctx-session-reads"


def session_reads_dir(workspace_root, *parts: str) -> str:
    """``<workspace_root>/.ctx-session-reads[/parts...]`` as a plain string.

    The single definition of the join. ``parts`` addresses a file or subtree
    *inside* the ledger directory (``"proxy", "window.json"``,
    ``"gate-fallback"``, ``f"{session_id}.count"``); ask for the subpath
    rather than re-deriving it from a separately-built parent.
    """
    return os.path.join(str(workspace_root), LEDGER_DIR_NAME, *parts)


def session_reads_path(workspace_root, *parts: str):
    """:func:`session_reads_dir` as a :class:`pathlib.Path`.

    A one-line adapter, not a second definition — every join still happens in
    :func:`session_reads_dir`. ``pathlib`` is imported here rather than at
    module scope because :mod:`ctx.hook` imports this module on its hot path
    and must not pay ~4 ms for ``pathlib`` per tool call; nothing on that path
    calls this function.
    """
    from pathlib import Path

    return Path(session_reads_dir(workspace_root, *parts))
