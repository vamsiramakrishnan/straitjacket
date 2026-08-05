"""Acceptance: call-graph edges outside Python.

v1 parsed only ``*.py`` with stdlib ``ast``, so nodes were polyglot (the
skeleton tier reads 20 extensions) while edges were not: ``ctx stats`` on a
Rust file returned its symbols and spans, and ``ctx callers`` on one of those
symbols answered "no definition ... in workspace Python sources".

v2 sources nodes from ``ctx.skeleton`` and call sites from one ast-grep
pattern, so both come from engines the repo already declares. The tier needs
the optional ``[code]`` extra; these tests skip without it, and the Python
path (tests/test_callgraph.py) is what must always work.
"""

import subprocess

import pytest

pytest.importorskip("ast_grep_py", reason="polyglot tier needs the [code] extra")
pytest.importorskip("tree_sitter", reason="polyglot tier needs the [code] extra")

SRC = {
    "ctx.toml": "version = 1\n",
    "src/lib.rs": (
        "fn helper(x: i32) -> i32 {\n"
        "    x + 1\n"
        "}\n"
        "\n"
        "fn caller() -> i32 {\n"
        "    helper(41)\n"
        "}\n"
    ),
    "src/main.go": (
        "package main\n"
        "\n"
        "func helperGo() int {\n"
        "\treturn 1\n"
        "}\n"
        "\n"
        "func callerGo() int {\n"
        "\treturn helperGo()\n"
        "}\n"
    ),
    "web/app.ts": (
        "export function helperTs(): number {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "export function callerTs(): number {\n"
        "  return helperTs();\n"
        "}\n"
    ),
}


@pytest.fixture()
def ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    for rel, content in SRC.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


@pytest.mark.parametrize(
    ("callee", "caller", "rel"),
    [
        ("helper", "caller", "src/lib.rs"),
        ("helperGo", "callerGo", "src/main.go"),
        ("helperTs", "callerTs", "web/app.ts"),
    ],
)
def test_edges_exist_outside_python(ws_store, callee, caller, rel):
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, callee)
    assert "no definition matching" not in out, f"{rel}: {callee} has no node"
    assert caller in out, f"{rel}: {caller} missing as a caller of {callee}"
    assert rel in out, f"{rel}: coordinates missing"


def test_same_file_calls_bind_at_the_local_tier(ws_store):
    """A callee defined in the calling file needs no import evidence, so
    polyglot edges are precise rather than repo-wide guesses."""
    from ctx.callgraph import _TIER_LOCAL, _load_graph

    ws, store = ws_store
    g = _load_graph(store, ws)
    # Node ids are file-scoped ("rel::qual"), so resolve names through the
    # node table rather than indexing the edge maps by bare qual.
    for callee, caller in (("helper", "caller"), ("helperGo", "callerGo")):
        edges = [
            e for nid, es in g.in_edges.items() if g.nodes[nid].qual == callee for e in es
        ]
        assert any(
            g.nodes[c].qual == caller and t == _TIER_LOCAL for c, _l, t in edges
        ), f"{callee} <- {caller} should be a local-tier edge, got {edges}"


def test_call_site_lines_are_real(ws_store):
    from ctx.callgraph import _load_graph

    ws, store = ws_store
    g = _load_graph(store, ws)
    # `helper(41)` is on line 6 of src/lib.rs.
    edges = [
        e for nid, es in g.in_edges.items() if g.nodes[nid].qual == "helper" for e in es
    ]
    assert any(g.nodes[c].qual == "caller" and line == 6 for c, line, _t in edges)


def test_engine_label_names_the_polyglot_rung(ws_store):
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, "helper")
    assert "skeleton+astgrep" in out, "the rung in force must be disclosed"


def test_python_only_mode_drops_the_polyglot_tier(ws_store, monkeypatch):
    """CTX_CALLGRAPH_ENGINE=ast pins the always-available rung — the v1 corpus,
    kept addressable for A/B and for hosts without the [code] extra."""
    from ctx.callgraph import cmd_callers

    monkeypatch.setenv("CTX_CALLGRAPH_ENGINE", "ast")
    ws, store = ws_store
    out = cmd_callers(store, ws, "helper")
    assert "no definition matching" in out
