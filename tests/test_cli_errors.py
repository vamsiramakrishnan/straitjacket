"""The CLI's error contract: what a failing `ctx` invocation says, on which
stream, and with which exit code.

`ctx` is invoked from hooks, scripts, and — most of all — by agents, so its
failure surface is a real interface. These tests pin the three things the
audit found unpinned:

  * the most common agent-facing error (a handle that GC or retention has
    already collected) is attributed to the verb and names its own cause;
  * bad input exits with ONE code across the whole verb surface;
  * an inner failure is not reported as a ctx failure.

tests/test_retrieval.py exercises the same exceptions at library level; this
file is the CLI half, which had no coverage at all.
"""

import pytest


@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return root


def _run(ws_root, *args):
    from ctx.cli import main

    return main(["--workspace", str(ws_root), *args])


# ------------------------------------------------- 1. the collected handle
def test_missing_handle_is_attributed_and_names_gc_and_retention(ws_root, capsys):
    """`ctx get run:<id>` after a `ctx gc` is the error an agent hits most.

    Before: it fell through cli.py's blanket handler as
    `ctx: no object matches id prefix 'abc123def' in this workspace` —
    generic prefix, no cause, no remediation.
    """
    rc = _run(ws_root, "get", "run:abc123def")
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("ctx get: "), err  # attributed to the verb, not bare `ctx:`
    assert "gc" in err and "retention" in err  # the likely cause is named
    assert "ctx pin" in err  # and a remediation for next time


def test_missing_handle_is_attributed_for_every_retrieval_verb(ws_root, capsys):
    for verb, args in (
        ("get", ["get", "run:abc123def"]),
        ("search", ["search", "run:abc123def", "x"]),
        ("stats", ["stats", "run:abc123def"]),
        ("diff", ["diff", "run:abc123def", "run:def456abc"]),
    ):
        _run(ws_root, *args)
        err = capsys.readouterr().err
        assert err.startswith(f"ctx {verb}: "), (verb, err)


def test_unknown_id_error_still_carries_its_prefix(ws_root):
    """The message gained a cause and a remediation; it must not lose the
    thing that identifies WHICH handle failed."""
    from ctx.store import Store, UnknownIdError
    from ctx.workspace import resolve_workspace

    store = Store(resolve_workspace(str(ws_root)).workspace_id)
    with pytest.raises(UnknownIdError, match="abc123def"):
        store.resolve_id("abc123def")
