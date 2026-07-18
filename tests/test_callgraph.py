"""Acceptance: the call graph (ctx callers/callees/impact) — zero-dep ast
edges, deterministic, worktree-hash cached, span-backed, ambiguity disclosed."""

import subprocess

import pytest

SRC = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "def leaf():\n"
        "    return 1\n\n\n"
        "def mid():\n"
        "    return leaf() + leaf()\n\n\n"
        "def top():\n"
        "    return mid()\n\n\n"
        "class Widget:\n"
        "    def render(self):\n"
        "        return leaf()\n"
    ),
    "pkg/other.py": (
        "from pkg.core import top\n\n\n"
        "def entry():\n"
        "    return top()\n\n\n"
        "def render():\n"  # a second 'render' → ambiguity
        "    return 0\n"
    ),
}


@pytest.fixture()
def ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel, content in SRC.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def test_callers_direct(ws_store):
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, "leaf")
    assert "callers (exact by name): 2" in out  # mid, Widget.render
    assert "mid" in out and "Widget.render" in out
    assert "pkg/core.py:" in out  # span-backed coordinates


def test_callees_in_repo_only(ws_store):
    from ctx.callgraph import cmd_callees

    ws, store = ws_store
    out = cmd_callees(store, ws, "mid")
    assert "leaf" in out  # mid calls leaf
    # 'return' and builtins are not in-repo defs and must not appear
    assert "print" not in out


def test_impact_transitive_blast_radius(ws_store):
    from ctx.callgraph import cmd_impact

    ws, store = ws_store
    out = cmd_impact(store, ws, "leaf", depth=6)
    # leaf <- mid <- top <- entry ; leaf <- Widget.render
    for node in ("mid", "top", "entry", "Widget.render"):
        assert node in out, f"{node} missing from blast radius"
    assert "transitive callers" in out


def test_impact_depth_bound(ws_store):
    from ctx.callgraph import cmd_impact

    ws, store = ws_store
    shallow = cmd_impact(store, ws, "leaf", depth=1)
    assert "mid" in shallow  # depth-1 caller
    assert "entry" not in shallow  # entry is 3 hops away, excluded at depth 1


def test_ambiguity_is_disclosed_not_hidden(ws_store):
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    # 'render' is defined twice (Widget.render and module-level render).
    out = cmd_callers(store, ws, "render")
    assert "ambiguous" in out
    assert "2 definitions" in out


def test_determinism_and_cache(ws_store):
    from ctx.callgraph import cmd_impact

    ws, store = ws_store
    a = cmd_impact(store, ws, "leaf")
    b = cmd_impact(store, ws, "leaf")
    assert a == b
    # cache file exists after first build
    cache_dir = store.root / "indexes" / "callgraph"
    assert cache_dir.is_dir() and any(cache_dir.iterdir())


def test_unknown_symbol_is_clean(ws_store):
    from ctx.callgraph import cmd_callers, cmd_impact

    ws, store = ws_store
    assert "no definition named" in cmd_callers(store, ws, "does_not_exist")
    assert "no definition named" in cmd_impact(store, ws, "does_not_exist")


def test_cli_wiring(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    assert main(["--workspace", str(ws.root), "impact", "leaf"]) == 0
    assert "transitive callers" in capsys.readouterr().out
