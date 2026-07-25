"""One fail-open reader for the Tier-0 proxy's ``window.json`` (R3).

The observer proxy writes ground truth about the session — context-window
fullness, model id, context limit, cumulative output tokens, request count,
contained bytes — to ``<workspace>/.ctx-session-reads/proxy/window.json``.
Five planes read it: the guard's window-pressure loop, the guard's price
note, the emission governor, the delivery-policy resolver, the engagement
model profile, and the status line.

Each carried its own ``open`` + ``json.load`` + bare ``except`` — six
copies of the same fail-open contract, differing only in which key they
pulled out afterwards.

**The contract, which every caller depends on and none may change:** the
file is written by a process the harness does not control and is *routinely
absent* — a plain ``ctx`` session with no proxy never creates it. Absence is
normal, not an error. So is a half-written file: the proxy writes atomically
(temp + rename), but a reader on another host, a truncated write, or a
hand-edited file must all degrade the same way. Every failure — missing
workspace root, missing file, IO error, malformed JSON, or a document that
is not a JSON object — yields the empty document. Nothing here raises, and
no pressure, nudge, or price is ever applied because telemetry broke.

Deliberately its own module rather than a function in :mod:`ctx.hook`: the
hook is the safety plane, and :mod:`ctx.resolver` must not create an import
edge into it (see that module's header). Both now depend on this instead.

Hot path: :mod:`ctx.hook` imports this on every intercepted tool call, so
module scope is ``json`` + ``os`` (via :mod:`ctx.sessiondir`) only — both
already loaded by the hook itself.
"""

from __future__ import annotations

import json

from ctx.sessiondir import session_reads_dir

__all__ = ["PROXY_SUBDIR", "WINDOW_FILENAME", "read_window_doc", "window_path"]

#: The proxy's state directory, inside the house ledger directory. Named here
#: because both ends of the contract need it: ``ctx wrap`` creates it and the
#: readers below look in it.
PROXY_SUBDIR = "proxy"
#: The snapshot file itself — :mod:`ctx.proxy` writes it (atomically, via a
#: sibling ``.tmp``), everything else only ever reads it.
WINDOW_FILENAME = "window.json"


def window_path(workspace_root) -> str:
    """Where the proxy writes its snapshot. Absolute, str."""
    return session_reads_dir(workspace_root, PROXY_SUBDIR, WINDOW_FILENAME)


def read_window_doc(workspace_root) -> dict:
    """The proxy's window document, or ``{}``.

    Fail-open by contract — see the module docstring. Callers pull their own
    keys out of the returned mapping and are responsible for their own type
    checks on the values, because the document is written by another process
    and a key may be present but nonsense (a string ``window_pct``, a boolean
    where a number belongs).
    """
    if not workspace_root:
        return {}
    try:
        with open(window_path(workspace_root), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}
