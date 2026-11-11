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

__all__ = ["LEDGER_DIR_NAME", "PRIVATE_LEDGERS", "private_ledger_dir",
           "session_reads_dir", "session_reads_path"]

#: The directory name itself. Use this when the *name* is what matters —
#: excluding a top-level component from a walk, a glob, or a listing.
LEDGER_DIR_NAME = ".ctx-session-reads"


#: Ledgers whose rows carry text taken from commands and tool inputs (run
#: signatures, ``ctx q`` pipelines, permission rules). Under the workspace
#: they made a later recursive content search match its own earlier
#: pattern, in a pipeline too, where nothing can filter it; and they kept
#: anything typed on a command line in a file under the repository. They
#: live in ctx's state directory instead, beside the artifact store, keyed by
#: the workspace, in a directory only the user can read.
PRIVATE_LEDGERS = frozenset({
    "reflex.json", "reflex.json.lock", "reflex-outcomes.jsonl",
    "interventions.jsonl", "interventions.cursor.json", "steering-shadow.jsonl",
    "steering-decisions.jsonl", "q-dry.json", "q-dry.jsonl", "ccperm-cache.json",
    # Holds the workspace's absolute path and the learned command signatures.
    "guard-policy-cache.json",
})

_PRIVATE_DIRS: dict[str, str] = {}


def private_ledger_dir(workspace_root) -> str:
    """``<state>/ledgers/<workspace key>``: where :data:`PRIVATE_LEDGERS`
    live. ``<state>`` is the artifact store's root (``CTX_STATE_HOME``, else
    ``$XDG_STATE_HOME/ctx``, else ``~/.local/state/ctx``)."""
    base = os.environ.get("CTX_STATE_HOME")
    if not base:
        xdg = os.environ.get("XDG_STATE_HOME")
        base = os.path.join(xdg or os.path.join(os.path.expanduser("~"), ".local", "state"), "ctx")
    key = str(workspace_root)
    digest = _PRIVATE_DIRS.get(key)
    if digest is None:
        import hashlib

        digest = hashlib.sha256(
            os.path.realpath(key).encode("utf-8", "surrogateescape")).hexdigest()[:20]
        _PRIVATE_DIRS[key] = digest
    return os.path.join(base, "ledgers", digest)


def _is_private(name: str) -> bool:
    if name in PRIVATE_LEDGERS:
        return True
    # Atomic-write temp names: `reflex.json.<pid>.<rand>`.
    return name.startswith(("reflex.json.", "guard-policy-cache.json."))


def session_reads_dir(workspace_root, *parts: str) -> str:
    """``<workspace_root>/.ctx-session-reads[/parts...]`` as a plain string.

    The single definition of the join. ``parts`` addresses a file or subtree
    *inside* the ledger directory (``"proxy", "window.json"``,
    ``"gate-fallback"``, ``f"{session_id}.count"``); ask for the subpath
    rather than re-deriving it from a separately-built parent. A file named
    in :data:`PRIVATE_LEDGERS` resolves to :func:`private_ledger_dir`.
    """
    if parts and _is_private(parts[0]):
        private = os.path.join(private_ledger_dir(workspace_root), *parts)
        if len(parts) == 1 and not os.path.lexists(private):
            _adopt_legacy(os.path.join(str(workspace_root), LEDGER_DIR_NAME, parts[0]), private)
        return private
    return os.path.join(str(workspace_root), LEDGER_DIR_NAME, *parts)


def _adopt_legacy(legacy: str, private: str) -> None:
    """Move a private ledger an older ctx left under the workspace to its
    private place, so the command text in it leaves the repository. A file
    only; fail-open (the ledgers are bookkeeping, never evidence)."""
    try:
        # Never through a link: neither the file nor the ledger directory.
        if (not os.path.isfile(legacy) or os.path.islink(legacy)
                or os.path.islink(os.path.dirname(legacy))):
            return
        os.makedirs(os.path.dirname(private), mode=0o700, exist_ok=True)
        try:
            os.replace(legacy, private)
        except OSError:  # another filesystem: copy, then remove
            with open(legacy, "rb") as src:
                data = src.read()
            fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dst:
                dst.write(data)
            os.unlink(legacy)
    except OSError:
        pass


def ledger_file(reads_dir, name: str):
    """The file ``name`` for a caller holding a ``.ctx-session-reads``
    directory: the private location for a private ledger (falling back to a
    copy left under the workspace by an older ctx), else ``reads_dir/name``."""
    from pathlib import Path

    reads_dir = Path(reads_dir)
    if _is_private(name):
        private = Path(session_reads_dir(reads_dir.parent, name))
        if private.exists() or not (reads_dir / name).exists():
            return private
    return reads_dir / name


def ensure_private_dir(path: str) -> None:
    """Create the parent of a ledger path; a private one only for the user."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)


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
