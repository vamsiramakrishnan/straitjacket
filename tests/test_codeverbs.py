"""M-B acceptance: symbol-addressed code verbs (def/refs/diag) with the
provenance contract — every emitted site snapshot-backed and span-tagged."""

import builtins
import re
import sys

import pytest

from conftest import make_store, make_ws


def _has_jedi() -> bool:
    try:
        import jedi  # noqa: F401

        return True
    except ImportError:
        return False


jedi_required = pytest.mark.skipif(not _has_jedi(), reason="jedi not installed")


def _seed_pkg(root):
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "storelike.py").write_text(
        "class StoreLike:\n"
        "    def put_blob(self, data):\n"
        "        return len(data)\n"
        "\n"
        "    def helper(self):\n"
        "        return self.put_blob(b'x')\n",
        encoding="utf-8",
    )
    (pkg / "user.py").write_text(
        "from pkg.storelike import StoreLike\n"
        "\n"
        "def use():\n"
        "    return StoreLike().put_blob(b'abc')\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------- def
@jedi_required
def test_def_resolves_method_with_snapshot_and_span(state_home, workspace_dir):
    from ctx.codeverbs import cmd_def
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_pkg(workspace_dir)

    out = cmd_def(store, ws, "repo:pkg/storelike.py:StoreLike.put_blob")
    assert "engine jedi" in out.splitlines()[0]
    assert "definition: repo:pkg/storelike.py L2:3" in out
    assert "snapshot: snapshot:" in out
    assert "def put_blob" in out

    # The minted span resolves through ctx get to the definition body.
    sid = re.search(r"span: (\w+)", out).group(1)
    got = get(store, ws, "repo:pkg/storelike.py", Selector(span=sid))
    assert "def put_blob" in got and "return len(data)" in got


def test_def_ast_engine_same_shape(state_home, workspace_dir, monkeypatch):
    from ctx.codeverbs import cmd_def

    monkeypatch.setenv("CTX_CODE_ENGINE", "ast")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_pkg(workspace_dir)

    out = cmd_def(store, ws, "repo:pkg/storelike.py:StoreLike.put_blob")
    assert "engine ast" in out.splitlines()[0]
    assert "definition: repo:pkg/storelike.py L2:3" in out
    assert "snapshot: snapshot:" in out
    assert "span: " in out
    assert "def put_blob" in out
    # --symbol style parses to the same target.
    out2 = cmd_def(store, ws, "repo:pkg/storelike.py --symbol StoreLike.put_blob")
    assert out == out2


def test_def_unknown_symbol_is_actionable(state_home, workspace_dir, monkeypatch):
    from ctx.codeverbs import cmd_def
    from ctx.retrieval import RetrievalError

    monkeypatch.setenv("CTX_CODE_ENGINE", "ast")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_pkg(workspace_dir)
    with pytest.raises(RetrievalError, match="not found"):
        cmd_def(store, ws, "repo:pkg/storelike.py:Missing.symbol")
    with pytest.raises(RetrievalError, match="grammar"):
        cmd_def(store, ws, "repo:pkg/storelike.py")


# --------------------------------------------------------------------- refs
def test_refs_three_sites_two_files_ast(state_home, workspace_dir, monkeypatch):
    from ctx.codeverbs import cmd_refs

    monkeypatch.setenv("CTX_CODE_ENGINE", "ast")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_pkg(workspace_dir)

    out = cmd_refs(store, ws, "put_blob", None)
    assert "engine ast (textual)" in out.splitlines()[0]
    assert "sites: 3 · shown: 3" in out
    lines = out.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("repo:")]
    assert [lines[i].split(":")[1] + ":" + lines[i].split(":")[2] for i in idx] == [
        "pkg/storelike.py:L2",
        "pkg/storelike.py:L6",
        "pkg/user.py:L4",
    ]
    assert "snapshots:" in out
    assert "pkg/storelike.py → snapshot:" in out
    assert "pkg/user.py → snapshot:" in out


@jedi_required
def test_refs_jedi_across_files(state_home, workspace_dir):
    from ctx.codeverbs import cmd_refs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_pkg(workspace_dir)

    out = cmd_refs(store, ws, "StoreLike.put_blob", None)
    assert "engine jedi" in out.splitlines()[0]
    assert "repo:pkg/storelike.py:L2:" in out
    assert "repo:pkg/user.py:L4:" in out
    assert out.index("repo:pkg/storelike.py:L2:") < out.index("repo:pkg/user.py:L4:")
    assert "snapshots:" in out


def test_refs_cap_and_continuation(state_home, workspace_dir, monkeypatch):
    from ctx.codeverbs import cmd_refs

    monkeypatch.setenv("CTX_CODE_ENGINE", "ast")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "many.py").write_text(
        "".join(f"needle_{i} = needle({i})\n" for i in range(90)), encoding="utf-8"
    )

    out = cmd_refs(store, ws, "needle", None)
    cap = ws.config.budgets.max_matches
    assert f"sites: 90 · shown: {cap} · truncated" in out
    assert "next: ctx refs needle" in out


# --------------------------------------------------------------------- diag
def _has_pyflakes() -> bool:
    import importlib.util

    return importlib.util.find_spec("pyflakes") is not None


@pytest.mark.skipif(not _has_pyflakes(), reason="pyflakes not installed")
def test_diag_pyflakes_unused_import(state_home, workspace_dir):
    from ctx.codeverbs import cmd_diag

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "bad.py").write_text("import os\n\nx = 1\n", encoding="utf-8")

    out = cmd_diag(store, ws, None)
    assert "checker pyflakes" in out.splitlines()[0]
    assert "warning 1" in out
    assert "repo:bad.py:L1: 'os' imported but unused" in out
    assert "checked: 1 of 1 python files" in out
    assert cmd_diag(store, ws, None) == out  # deterministic


def test_diag_py_compile_fallback(state_home, workspace_dir, monkeypatch):
    import ctx.codeverbs as codeverbs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(codeverbs, "_pyflakes_available", lambda: False)

    out = codeverbs.cmd_diag(store, ws, None)
    assert "checker py_compile" in out.splitlines()[0]
    assert "error 1" in out
    assert "repo:broken.py:L1:" in out


# -------------------------------------------------------------- determinism
def test_def_and_refs_byte_identical_across_invocations(state_home, workspace_dir):
    from ctx.codeverbs import cmd_def, cmd_refs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_pkg(workspace_dir)

    d1 = cmd_def(store, ws, "repo:pkg/storelike.py:StoreLike.put_blob")
    d2 = cmd_def(store, ws, "repo:pkg/storelike.py:StoreLike.put_blob")
    assert d1 == d2
    r1 = cmd_refs(store, ws, "put_blob", None)
    r2 = cmd_refs(store, ws, "put_blob", None)
    assert r1 == r2


# ---------------------------------------------------------------------- mcp
def test_mcp_dispatch_def(state_home, workspace_dir):
    from ctx.mcp import _dispatch

    _seed_pkg(workspace_dir)
    out = _dispatch(
        {
            "op": "def",
            "workspace": str(workspace_dir),
            "options": {"target": "repo:pkg/storelike.py:StoreLike.put_blob"},
        }
    )
    assert out.startswith("[ctx def repo:pkg/storelike.py:StoreLike.put_blob")
    assert "engine" in out.splitlines()[0]
    assert "snapshot: snapshot:" in out


# ------------------------------------------------------------- jedi absence
def test_jedi_absent_degrades_to_ast_with_disclosure(
    state_home, workspace_dir, monkeypatch
):
    from ctx.codeverbs import cmd_def, cmd_refs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_pkg(workspace_dir)

    real_import = builtins.__import__

    def no_jedi(name, *args, **kwargs):
        if name == "jedi" or name.startswith("jedi."):
            raise ImportError("jedi disabled for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "jedi", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_jedi)

    out = cmd_def(store, ws, "repo:pkg/storelike.py:StoreLike.put_blob")
    assert "engine ast" in out.splitlines()[0]
    assert "def put_blob" in out
    refs = cmd_refs(store, ws, "put_blob", None)
    assert "engine ast (textual)" in refs.splitlines()[0]
    assert "sites: 3" in refs
