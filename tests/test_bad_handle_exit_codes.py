"""A handle that does not resolve is a rejected INVOCATION, not a crash.

docs/CLI.md's exit-code table reserves 2 for "ctx rejected the invocation"
-- a handle collected by `ctx gc` or a retention window is the canonical
case -- and 1 for "ctx failed", an internal error. A calling script tells
those apart by the code alone.

The classification lived inside `commands/retrieve.py` and was imported
from `admin.py` across the module boundary. `cmd_pin` picked it up;
`cmd_checkpoint --show` did not, so the same unresolvable handle exited 2
through `ctx get` and 1 through `ctx checkpoint` -- a third door onto one
guard, found by a bug bash.

The fix moved the classification to a module of its own. This test is the
part that keeps it: every verb that accepts a handle is exercised with a
well-formed one that resolves to nothing, and must answer 2. A new
handle-taking verb belongs in HANDLE_VERBS the day it is added.
"""

from __future__ import annotations

import pytest

#: A syntactically valid ref whose id is not in any store.
GHOST = "0123456789abcdef0123456789abcdef01234567"

#: verb -> argv after `--workspace <dir>`. Every entry takes a HANDLE, so
#: every entry owes the same exit code for an unresolvable one.
HANDLE_VERBS: dict[str, list[str]] = {
    "get": ["get", f"blob:{GHOST}"],
    "stats": ["stats", f"run:{GHOST}"],
    "search": ["search", f"run:{GHOST}", "x"],
    "diff": ["diff", f"run:{GHOST}", f"run:{GHOST}"],
    "pin": ["pin", f"blob:{GHOST}"],
    "checkpoint": ["checkpoint", "--show", f"checkpoint:{GHOST}"],
}


@pytest.mark.parametrize("verb", sorted(HANDLE_VERBS))
def test_unresolvable_handle_exits_two(verb, state_home, workspace_dir, capsys):
    from ctx.cli import main as cli_main

    rc = cli_main(["--workspace", str(workspace_dir), *HANDLE_VERBS[verb]])
    err = capsys.readouterr().err
    assert rc == 2, f"{verb!r} answered {rc}, not the documented 2 ({err!r})"
    assert f"ctx {verb}:" in err, (
        f"{verb!r} must attribute the failure to the verb the user typed: {err!r}"
    )


def test_the_classification_has_one_home():
    """It used to live in retrieve.py and be imported from admin.py. A
    mechanism that lives inside one of its callers is one someone else has
    to notice; that is exactly how cmd_checkpoint missed it."""
    from ctx.commands._errors import bad_input_errors, fail

    assert callable(bad_input_errors) and callable(fail)
    from ctx.refs import RefError
    from ctx.retrieval import RetrievalError
    from ctx.store import StoreError

    classes = bad_input_errors()
    assert RetrievalError in classes and RefError in classes and StoreError in classes


def test_a_genuine_internal_error_is_still_one():
    """2 must not swallow 1. The distinction is the whole point: an
    unresolvable handle is the caller's problem, an internal failure is
    ours, and a script has only the exit code to tell them apart."""
    from ctx.commands._errors import bad_input_errors

    assert not issubclass(RuntimeError, bad_input_errors())
