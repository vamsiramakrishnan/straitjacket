"""Regression tests for PR #1 review findings (Codex): ws: alias routing,
file selectors in the Python fallback, lease-aware garbage collection."""

import sys
import time

import pytest

from conftest import make_store, make_ws


def test_ws_alias_routes_to_target_workspace(state_home, tmp_path):
    from ctx.retrieval import search
    from ctx.workspace import resolve_workspace

    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    (b / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (a / "ctx.toml").write_text(
        f'version = 1\n[aliases]\napi = "{b}"\n', encoding="utf-8"
    )
    (a / "here.txt").write_text("needle in A\n", encoding="utf-8")
    (b / "there.txt").write_text("needle in B\n", encoding="utf-8")

    ws_a = resolve_workspace(str(a))
    store_a = make_store(ws_a)
    out = search(store_a, ws_a, "ws:api/repo:", ["needle"])
    assert "needle in B" in out and "there.txt" in out
    assert "needle in A" not in out  # never silently searches the current workspace


def test_ws_alias_unknown_is_rejected_not_guessed(state_home, workspace_dir):
    from ctx.retrieval import RetrievalError, search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    with pytest.raises(RetrievalError, match="unknown workspace alias"):
        search(store, ws, "ws:nope/repo:", ["x"])


def test_file_selector_python_fallback(state_home, workspace_dir, monkeypatch):
    from ctx.retrieval import search, stats

    monkeypatch.setenv("CTX_SEARCH_ENGINE", "python")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    (workspace_dir / "b.py").write_text("alpha = 2\n", encoding="utf-8")

    out = search(store, ws, "repo:a.py", ["alpha"])
    assert "a.py" in out and "matches: 1" in out
    assert "b.py" not in out

    st = stats(store, ws, "repo:a.py")
    # Since the priced-context wave, stats on a single .py file returns the
    # priced outline (still a non-empty single-file corpus, the original
    # regression this test guards).
    assert "[ctx stats repo:a.py]" in st
    assert "file (exact): 1 lines" in st


def test_file_selector_git_workspace_fallback(state_home, git_workspace, monkeypatch):
    from ctx.retrieval import search

    monkeypatch.setenv("CTX_SEARCH_ENGINE", "python")
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(git_workspace))
    store = make_store(ws)
    out = search(store, ws, "repo:hello.py", ["hello"])
    assert "hello.py" in out and "matches: 1" in out


def test_gc_honors_unexpired_leases(state_home, workspace_dir):
    import sys as _sys

    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = run_capture(ws, [_sys.executable, "-c", "print('leased')"], store=store)
    mid = cap.manifest_id

    # Age everything far past the recency cutoff; the retention lease is
    # still unexpired, so the manifest and its blobs must survive.
    with store.db:
        store.db.execute("UPDATE objects SET created_at = 0")
    store.gc(retention_days=1)
    assert store.get_manifest(mid)["schema"] == "ctx.invocation/v1"

    # Expire the lease: now gc may collect it.
    with store.db:
        store.db.execute("UPDATE leases SET expires_at = 1 WHERE expires_at IS NOT NULL")
    store.gc(retention_days=1)
    from ctx.store import UnknownIdError

    with pytest.raises(UnknownIdError):
        store.get_manifest(mid)
