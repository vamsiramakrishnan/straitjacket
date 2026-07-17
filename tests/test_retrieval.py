"""Acceptance: bounded search/get/stats, snapshot-on-read, budgets, handles."""

import sys

import pytest

from conftest import make_store, make_ws


def _capture_lines(ws, store, n=50, prefix="line"):
    from ctx.execution import run_capture

    script = f"[print(f'{prefix} {{i}}') for i in range({n})]"
    return run_capture(ws, [sys.executable, "-c", script], store=store)


def test_search_run_multi_pattern_deterministic(state_home, workspace_dir):
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_lines(ws, store)
    short = cap.manifest_id[:12]
    out1 = search(store, ws, f"run:{short}", ["line 7$", "line 12$"])
    out2 = search(store, ws, f"run:{short}", ["line 7$", "line 12$"])
    assert out1 == out2
    assert "L8: line 7" in out1  # 0-indexed print → line 8
    assert "matches: 2" in out1
    assert "scanned:" in out1


def test_search_repo_snapshot_on_read_survives_mutation(state_home, workspace_dir):
    from ctx.retrieval import Selector, get, search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    f = workspace_dir / "app.py"
    f.write_text("def handler():\n    raise TimeoutError('risk-api')\n", encoding="utf-8")

    out = search(store, ws, "repo:", ["TimeoutError"], glob="**/*.py")
    assert "app.py" in out and "snapshot:" in out
    snap_id = out.split("snapshot:")[-1].split()[0].strip()

    # Mutate the working file, then retrieve the snapshot: original bytes.
    f.write_text("completely different\n", encoding="utf-8")
    got = get(store, ws, f"snapshot:{snap_id}", Selector(lines=(1, 2)))
    assert "TimeoutError" in got
    assert "divergence" in got


def test_get_lines_bounded_with_continuation(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_lines(ws, store, n=2000)
    short = cap.manifest_id[:12]
    out = get(store, ws, f"run:{short}#stdout", Selector(lines=(1, 2000)))
    # max_inline_lines default is 240: oversized request must not flood.
    assert "--lines 241:" in out
    assert out.count("\nL") <= 245


def test_get_json_pointer(state_home, workspace_dir):
    from ctx.execution import run_capture
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = "import json; print(json.dumps({'items': [{'name': 'a'}, {'name': 'b'}]}))"
    cap = run_capture(ws, [sys.executable, "-c", script], store=store)
    out = get(store, ws, f"run:{cap.manifest_id[:12]}#stdout", Selector(json_pointer="/items/1/name"))
    assert '"b"' in out


def test_ambiguous_short_id_refused(state_home, workspace_dir):
    from ctx.store import AmbiguousIdError

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    # Two real colliding hashes are impractical; inject catalog rows directly
    # to simulate the prefix collision.
    with store.db:
        store.db.execute(
            "INSERT INTO objects (id, kind, created_at, meta) VALUES (?, 'blob', 0, '{}')",
            ("abcdef" + "b" * 58,),
        )
        store.db.execute(
            "INSERT INTO objects (id, kind, created_at, meta) VALUES (?, 'blob', 0, '{}')",
            ("abcdef" + "c" * 58,),
        )
    with pytest.raises(AmbiguousIdError):
        store.resolve_id("abcdef")


def test_handles_scoped_to_workspace(state_home, tmp_path):
    from ctx.store import Store, UnknownIdError

    from ctx.workspace import resolve_workspace

    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    for d in (ws_a, ws_b):
        d.mkdir()
        (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    a = resolve_workspace(str(ws_a))
    b = resolve_workspace(str(ws_b))
    store_a = Store(a.workspace_id)
    store_b = Store(b.workspace_id)
    blob = store_a.put_blob(b"scoped-payload")
    store_a.get_blob(blob)
    with pytest.raises((UnknownIdError, Exception)):
        store_b.get_blob(blob)


def test_stats_repo_labeled_exact(state_home, workspace_dir):
    from ctx.retrieval import stats

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "m.py").write_text("x = 1\n", encoding="utf-8")
    out = stats(store, ws, "repo:")
    assert "(exact)" in out
    assert "python" in out
    assert str(workspace_dir) not in out


def test_search_max_matches_truncation_reported(state_home, workspace_dir):
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_lines(ws, store, n=300, prefix="hit")
    out = search(store, ws, f"run:{cap.manifest_id[:12]}", ["hit"], max_matches=10)
    assert "shown: 10" in out
    assert "truncated" in out


def test_secret_paths_excluded_from_capture(state_home, workspace_dir):
    from ctx.execution import ExecutionError, snapshot_file

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    secret = workspace_dir / ".env"
    secret.write_text("TOKEN=abc\n", encoding="utf-8")
    with pytest.raises(ExecutionError):
        snapshot_file(store, ws, ".env")


def test_redaction_marks_but_never_prints_secret(state_home, workspace_dir):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    script = f"print('ERROR failed with key {key}')"
    cap = run_capture(ws, [sys.executable, "-c", script], store=store)
    digest, _ = render_run_digest(store, ws, cap.manifest)
    assert key not in digest
    assert "ctx:redacted:aws-access-key" in digest
    assert "redaction: applied" in digest
    # Raw artifact remains byte-exact.
    raw = store.get_blob(cap.manifest["streams"]["stdout"]["blob"].removeprefix("sha256:"))
    assert key.encode() in raw


def test_gc_retains_pinned(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    keep = store.put_blob(b"keep-me")
    drop = store.put_blob(b"drop-me")
    store.pin(keep)
    # Age both objects far past retention.
    with store.db:
        store.db.execute("UPDATE objects SET created_at = 0")
    result = store.gc(retention_days=1)
    assert result["blobs_removed"] == 1
    assert store.get_blob(keep) == b"keep-me"


def test_turn_budget_enforced(state_home, workspace_dir, monkeypatch):
    from ctx.retrieval import charge_turn_budget

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    monkeypatch.setenv("CTX_CONVERSATION_ID", "conv1")
    monkeypatch.setenv("CTX_TURN_ID", "t1")
    big = "x" * (ws.config.budgets.turn_retrieval_tokens * 4 + 100)
    warning = charge_turn_budget(store, ws, big)
    assert warning is not None and "budget exceeded" in warning
