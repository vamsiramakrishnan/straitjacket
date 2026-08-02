"""The rewrite guard refuses what it cannot verify.

astgrep.py's docstring promises `rewrite_apply` "refuses if the source-state
generation changed since preview". The check was
`if expect_generation and gen_now != expect_generation`, so a FALSY
expectation skipped it entirely -- and a bug bash found two independent ways
to produce one:

* a non-git workspace, where generation_hash fail-opens to None by contract;
* a plan step whose upstream op simply carried no `generation` key.

Same falsy value, same skipped guard, two doors. Plus a third defect in the
same module: the patch text was built from a lossily-decoded file, so one
stray non-UTF-8 byte in a context line produced a confident preview whose
patch `git apply` then rejected -- in a module whose docstring promises no
lossy fallback.
"""

from __future__ import annotations

import pytest
from conftest import make_store, make_ws


def test_guard_state_is_never_none_without_git(tmp_path, state_home):
    from ctx.astgrep import _guard_state

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ws = make_ws(tmp_path)
    assert ws.git is None, "this test is about the non-git path"
    state = _guard_state(ws)
    assert state and isinstance(state, str)


def test_guard_state_changes_when_any_file_changes(tmp_path, state_home):
    """WORKTREE scope, not patch-target scope. Narrowing it to the files the
    patch touches is cheaper and would stop an unrelated edit invalidating a
    good patch -- but the documented guarantee is about the source state, and
    quietly making a safety guard accept more than it says it accepts is the
    failure this fix is about."""
    import time

    from ctx.astgrep import _guard_state

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ws = make_ws(tmp_path)
    before = _guard_state(ws)

    time.sleep(0.01)
    (tmp_path / "unrelated.py").write_text("y = 2\n", encoding="utf-8")
    assert _guard_state(ws) != before, "an edit ANYWHERE changes the state"


def test_apply_refuses_without_an_expectation(tmp_path, state_home):
    """`_op_rewrite_apply` computed `args.get("generation") or
    meta_in.get("generation")`, which is None for plenty of legal upstream
    ops -- and None skipped the check instead of failing it."""
    from ctx.astgrep import RewriteError, rewrite_apply

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = make_ws(tmp_path)
    store = make_store(ws)
    patch = store.put_blob(
        b"--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    with pytest.raises(RewriteError) as e:
        rewrite_apply(ws, store, patch, None)
    assert "cannot be checked" in str(e.value)


def test_apply_refuses_a_stale_expectation(tmp_path, state_home):
    from ctx.astgrep import RewriteError, rewrite_apply

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = make_ws(tmp_path)
    store = make_store(ws)
    patch = store.put_blob(
        b"--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    with pytest.raises(RewriteError) as e:
        rewrite_apply(ws, store, patch, "gen:something-else")
    assert "generation changed" in str(e.value)


def test_empty_patch_is_still_a_no_op_not_a_refusal(tmp_path, state_home):
    """An empty patch changes nothing, so there is nothing to be stale about.
    The guard must not turn a harmless no-op into an error."""
    from ctx.astgrep import rewrite_apply

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = make_ws(tmp_path)
    store = make_store(ws)
    rows, meta = rewrite_apply(ws, store, store.put_blob(b"   \n"), None)
    assert rows == [] and meta["applied_files"] == 0


def test_patch_targets_are_read_from_the_diff():
    from ctx.astgrep import _patch_targets

    patch = (
        b"--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n"
        b"--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-c\n+d\n"
    )
    assert _patch_targets(patch) == ["one.py", "two.py"]


def test_patch_targets_survive_non_utf8_bytes():
    """The parse runs on the same byte-exact codec the patch is built with,
    so a diff carrying arbitrary bytes still names its own files."""
    from ctx.astgrep import _patch_targets

    patch = b"--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n-\xff\xfe\n+ok\n"
    assert _patch_targets(patch) == ["m.py"]
