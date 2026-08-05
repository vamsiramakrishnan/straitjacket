"""One classification of a bad-input failure, for every command.

docs/CLI.md's exit-code table reserves 2 for "ctx rejected the invocation"
-- a handle that no longer resolves because ``ctx gc`` or the retention
window collected it is the canonical case -- and 1 for "ctx failed", an
internal error. A calling script tells those apart by the code alone.

This lived in ``commands/retrieve.py`` and was imported from ``admin.py``
across the module boundary, which is what a shared mechanism living in one
caller looks like. ``cmd_pin`` picked it up; ``cmd_checkpoint --show`` did
not, so the same unresolvable handle exited 2 through ``ctx get`` and 1
through ``ctx checkpoint``. Third door onto one guard.
"""

from __future__ import annotations

import sys


def bad_input_errors() -> tuple[type[BaseException], ...]:
    """The exception classes a verb must answer for itself.

    ``RetrievalError`` alone was not enough: ``UnknownIdError`` (and its
    sibling ``AmbiguousIdError``) subclass ``StoreError``, and ``parse_ref``
    raises ``RefError`` -- so the single most common agent-facing mistake,
    ``ctx get run:<id>`` after a ``ctx gc`` or a retention expiry, fell
    through to cli.py's blanket handler and printed a bare ``ctx: ...`` with
    no verb attribution.
    """
    from ctx.refs import RefError
    from ctx.retrieval import RetrievalError
    from ctx.store import StoreError

    return (RetrievalError, RefError, StoreError)


def fail(verb: str, e: BaseException) -> int:
    """One error tail for every verb: attribute the failure to the verb the
    user typed, and return the documented exit code.

    Exit 2, not 1. All three classes mean the same thing to a caller -- ctx
    rejected what it was given -- and the code used to depend purely on
    which verb family caught them: ``ctx get`` said 1 for a bad selector
    while ``ctx q`` and argparse said 2 for the same class of mistake. 2 is
    the argparse convention and already the majority of this codebase's own
    usage errors, so 1 is left to mean only "ctx itself failed" -- the
    blanket handler in cli.py.
    """
    print(f"ctx {verb}: {e}", file=sys.stderr)
    return 2


__all__ = ["bad_input_errors", "fail"]
