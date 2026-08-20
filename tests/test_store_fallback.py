from __future__ import annotations

import json
from pathlib import Path


def _register(tmp_path: Path, monkeypatch, *, backend: str = "user-state") -> tuple[str, Path]:
    import ctx.store as store_module

    workspace = tmp_path / "project"
    workspace.mkdir()
    user_state = tmp_path / "user-state"
    monkeypatch.setenv("CTX_STATE_HOME", str(user_state))
    workspace_id = "ws_store_fallback_test"
    store_module._STORE_POLICIES.clear()
    store_module._STORE_LOCATIONS.clear()
    store_module.register_workspace_store(workspace_id, workspace, backend)
    return workspace_id, workspace


def test_read_only_user_state_falls_back_and_records_sticky_route(tmp_path, monkeypatch):
    import ctx.store as store_module

    workspace_id, workspace = _register(tmp_path, monkeypatch)
    real_probe = store_module._probe_writable

    def fail_user_state(root: Path) -> None:
        state_root = Path(store_module.default_state_root())
        if root == state_root or state_root in root.parents:
            raise OSError(30, "read-only file system")
        real_probe(root)

    monkeypatch.setattr(store_module, "_probe_writable", fail_user_state)
    store = store_module.Store(workspace_id)

    assert store.location.backend == "workspace-local-fallback"
    assert store.root == workspace / ".ctx-session-reads" / "store" / "workspaces" / workspace_id
    marker = workspace / ".ctx-session-reads" / "store-backend.json"
    assert json.loads(marker.read_text()) == {
        "backend": "workspace-local-fallback",
        "version": 1,
    }
    assert store.get_blob(store.put_blob(b"retrievable")) == b"retrievable"


def test_fallback_marker_keeps_later_process_on_same_store(tmp_path, monkeypatch):
    import ctx.store as store_module

    workspace_id, workspace = _register(tmp_path, monkeypatch)
    marker = workspace / ".ctx-session-reads" / "store-backend.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"backend":"workspace-local-fallback","version":1}\n')

    store = store_module.Store(workspace_id)

    assert store.location.backend == "workspace-local-fallback"
    assert store.location.sticky is True
    assert store.location.detail == "workspace-local fallback; sticky for retrieval continuity"


def test_local_backend_is_now_effective_not_advisory(tmp_path, monkeypatch):
    import ctx.store as store_module

    workspace_id, workspace = _register(tmp_path, monkeypatch, backend="local")
    store = store_module.Store(workspace_id)

    assert store.location.backend == "workspace-local"
    assert store.root.parent.parent == workspace / ".ctx-session-reads" / "store"


def test_doctor_reports_effective_fallback_backend(tmp_path, monkeypatch):
    import ctx.store as store_module
    from ctx.installer import doctor_checks
    from ctx.workspace import resolve_workspace

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "user-state"))
    real_probe = store_module._probe_writable

    def fail_user_state(root: Path) -> None:
        state_root = Path(store_module.default_state_root())
        if root == state_root or state_root in root.parents:
            raise OSError(30, "read-only file system")
        real_probe(root)

    monkeypatch.setattr(store_module, "_probe_writable", fail_user_state)
    ws = resolve_workspace(str(workspace))
    row = next(row for row in doctor_checks(ws) if row[0] == "store writable")

    assert row == ("store writable", True, "workspace-local fallback")


def test_catalog_initialization_retries_a_parallel_lock(tmp_path, monkeypatch):
    import ctx.store as store_module

    store = store_module.Store("ws_parallel_catalog", state_root=tmp_path / "state")
    real_connect = store_module.sqlite3.connect
    attempts = 0

    class LockedConnection:
        def execute(self, _statement):
            raise store_module.sqlite3.OperationalError("database is locked")

        def close(self):
            return None

    def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return LockedConnection()
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)

    assert store.get_blob(store.put_blob(b"parallel-safe")) == b"parallel-safe"
    assert attempts == 2
