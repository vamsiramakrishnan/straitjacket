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


# ------------------------------------------- 2. truncation keeps the handle
def _tiny_budget_ws(root, *, engagement="active"):
    (root / "ctx.toml").write_text(
        "version = 1\n"
        "[budgets]\ndigest_tokens = 60\nresult_tokens = 60\n"
        f'[engagement]\nmode = "{engagement}"\n',
        encoding="utf-8",
    )
    return root


NOISY = "for i in range(400): print('line %d some content here to fill space' % i)"


@pytest.mark.parametrize("engagement", ["active", "passive"])
def test_truncated_run_digest_still_ends_with_a_retrieval_handle(
    ws_root, capsys, engagement
):
    """bounded() cuts from the bottom and `next:` is last in every profile,
    so the clamp deleted the retrieval affordance exactly when it was needed.
    filter_digest compounded it: cap 0 (passive / lean-model sessions, the
    default) dropped the whole block before the clamp ever ran."""
    import sys

    _tiny_budget_ws(ws_root, engagement=engagement)
    rc = _run(ws_root, "run", "--", sys.executable, "-c", NOISY)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ctx:truncated" in out, out  # precondition: it really was cut
    last = [ln for ln in out.strip().splitlines() if ln.strip()][-1]
    assert last.startswith("next: "), out  # a handle, not a dead end
    assert "ctx " in last  # ...carrying a verb
    assert "run:" in last  # ...and an address


def test_untruncated_digest_gains_no_extra_next_line(ws_root, capsys):
    """The handle is a truncation remedy, not a new affordance: a digest that
    fits must stay byte-identical, and a passive session must still get no
    suggestions."""
    import sys

    (ws_root / "ctx.toml").write_text(
        'version = 1\n[engagement]\nmode = "passive"\n', encoding="utf-8"
    )
    rc = _run(ws_root, "run", "--", sys.executable, "-c", "print('ok')")
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ctx:truncated" not in out
    assert "next:" not in out


def test_truncation_handle_reads_through_the_engagement_filter():
    """The handle is read from the UNFILTERED digest, so cap 0 cannot hide
    it, and it falls back to the first artifact handle when a digest carries
    no `next:` block at all."""
    from ctx.commands.emit import _truncation_handle

    digest = (
        "[ctx run:abc123abc123 profile=text/v1]\nbody\nnext:\n"
        "  ctx get run:abc123abc123#stdout --lines 6:395\n"
        "  ctx search run:abc123abc123 '<pattern>'\n"
    )
    assert _truncation_handle(digest) == "ctx get run:abc123abc123#stdout --lines 6:395"
    assert _truncation_handle("[ctx run:abc123abc123]\nbody\n") == (
        "ctx get run:abc123abc123"
    )
    assert _truncation_handle("no handle anywhere") is None


def test_bounded_truncation_continuation_only_fires_on_a_cut():
    from ctx.textutil import bounded

    assert bounded("short", 100, truncation_continuation="ctx get run:x") == "short"
    cut = bounded("x" * 4000, 10, truncation_continuation="ctx get run:x")
    assert cut.endswith("next: ctx get run:x")
