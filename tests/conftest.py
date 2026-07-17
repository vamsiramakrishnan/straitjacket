import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture()
def state_home(tmp_path, monkeypatch):
    """Isolated artifact store per test (never the user's real state dir)."""
    state = tmp_path / "state"
    monkeypatch.setenv("CTX_STATE_HOME", str(state))
    monkeypatch.delenv("CTX_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("CTX_TURN_ID", raising=False)
    return state


@pytest.fixture()
def workspace_dir(tmp_path):
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return ws


@pytest.fixture()
def git_workspace(tmp_path):
    ws = tmp_path / "gitproj"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / "hello.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    return ws


def make_ws(root: Path):
    from ctx.workspace import resolve_workspace

    return resolve_workspace(str(root))


def make_store(ws, state_home: Path | None = None):
    """Store bound to the CTX_STATE_HOME set by the state_home fixture."""
    from ctx.store import Store

    return Store(ws.workspace_id)
