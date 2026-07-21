"""Acceptance: the tree-sitter skeleton backend via grammar wheels.

The skeleton tier's preferred extractor (tree-sitter → ctags → stdlib ast)
now has a third, offline-safe backend: individual `tree_sitter_<lang>`
grammar wheels through the modern core API (the bundle language-pack
fetches parsers at runtime — a sandbox 403). With a grammar wheel present,
tree-sitter carries a JS/TS skeleton that stdlib `ast` cannot parse and
ctags need not; absence degrades down the chain, never errors."""

from __future__ import annotations

import pytest

from conftest import make_store, make_ws

HAS_TS_PY = True
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_python  # noqa: F401
except Exception:
    HAS_TS_PY = False

HAS_TS_JS = True
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_javascript  # noqa: F401
except Exception:
    HAS_TS_JS = False


@pytest.mark.skipif(not HAS_TS_PY, reason="tree-sitter-python grammar wheel absent")
def test_grammar_wheel_parser_builds():
    from ctx.skeleton import _ts_grammar_parser

    parser = _ts_grammar_parser("python")
    assert parser is not None
    tree = parser.parse(b"def foo(x):\n    return x\n")
    assert tree.root_node.children[0].type == "function_definition"


@pytest.mark.skipif(not HAS_TS_PY, reason="tree-sitter-python grammar wheel absent")
def test_ts_parser_falls_through_to_grammar_wheel():
    """_ts_parser tries the bundles first (absent here) then grammar
    wheels — it must return a working parser, not raise."""
    from ctx.skeleton import _ts_parser

    parser = _ts_parser("python")
    assert parser.parse(b"x = 1\n").root_node.type == "module"


@pytest.mark.skipif(not HAS_TS_JS, reason="tree-sitter-javascript grammar wheel absent")
def test_tree_sitter_carries_js_skeleton_without_ctags(
    state_home, workspace_dir, monkeypatch
):
    """A JS file (stdlib ast can't parse it) yields a real skeleton from
    tree-sitter with ctags disabled — proving the grammar-wheel backend is
    load-bearing, not just importable."""
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    from ctx.skeleton import skeleton_for

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "app.js").write_text(
        "export function greet(name) {\n  return 'hi ' + name;\n}\n"
        "const answer = 42;\n", encoding="utf-8",
    )
    sk = skeleton_for(store, ws, "app.js")
    syms = sk.get("symbols") if isinstance(sk, dict) else None
    names = {s.get("name") for s in (syms or [])}
    assert "greet" in names  # tree-sitter extracted the function
