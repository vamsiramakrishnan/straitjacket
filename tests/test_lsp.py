"""The language-server tier: a JSON-RPC stdio client that answers the
semantic questions a parser cannot (definition across files, references,
hover), fail-open and disclosed.

The fake server in tests/fake_lsp_server.py is registered through
CTX_LSP_SERVERS so the tests never depend on a real server; a real one
(pyright) is exercised only when it is on PATH.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).with_name("fake_lsp_server.py")


@pytest.fixture()
def fake_registry(monkeypatch):
    monkeypatch.setenv("CTX_LSP_SERVERS", json.dumps({"python": [[sys.executable, str(FAKE)]]}))
    monkeypatch.setenv("CTX_LSP_SETTLE_S", "0")  # the fake never publishes diagnostics


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("def hello(name):\n    return name\n\nprint(hello('x'))\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("hello\n", encoding="utf-8")
    return tmp_path


def test_registry_override_and_server_lookup(fake_registry):
    from ctx.lsp import registry, server_for

    assert registry() == {"python": [[sys.executable, str(FAKE)]]}
    assert server_for("python")[1] == str(FAKE)
    assert server_for("go") is None  # not in the override table
    assert server_for(None) is None


def test_default_registry_names_every_skeleton_language(monkeypatch):
    monkeypatch.delenv("CTX_LSP_SERVERS", raising=False)
    from ctx.lsp import SERVERS
    from ctx.skeleton import _LANG_BY_EXT

    assert set(_LANG_BY_EXT.values()) <= set(SERVERS)


def test_definition_references_hover_through_the_client(fake_registry, ws):
    from ctx.lsp import query

    sites, server = query(ws, "mod.py", 4, 7, "definition")
    assert server == Path(sys.executable).name
    assert sites == [("mod.py", 1, "def hello(name):")]

    sites, _ = query(ws, "mod.py", 4, 7, "references")
    # Workspace-confined, deduplicated, sorted; the outside file is dropped.
    assert sites == [("mod.py", 1, "def hello(name):"), ("mod.py", 3, ""), ("other.py", 1, "hello")]

    text, _ = query(ws, "mod.py", 1, 5, "hover")
    assert text == "def hello(name: str) -> str"


def test_no_server_for_language_raises_lsperror(monkeypatch, ws):
    from ctx.lsp import LspError, query

    monkeypatch.setenv("CTX_LSP_SERVERS", json.dumps({"python": [["definitely-not-a-server-binary"]]}))
    with pytest.raises(LspError, match="no language server"):
        query(ws, "mod.py", 1, 5, "hover")


def test_crashing_server_is_a_disclosed_failure_not_a_hang(monkeypatch, ws, tmp_path):
    from ctx.lsp import LspError, query

    crash = tmp_path / "crash.py"
    crash.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    monkeypatch.setenv("CTX_LSP_SERVERS", json.dumps({"python": [[sys.executable, str(crash)]]}))
    with pytest.raises(LspError):
        query(ws, "mod.py", 1, 5, "hover", timeout=5)


def test_symbol_position_from_skeleton(state_home, ws):
    from ctx.lsp import symbol_position

    assert symbol_position(ws, "mod.py", "hello") == (1, 5)
    assert symbol_position(ws, "mod.py", "nope") is None


def test_cli_lsp_verb_renders_sites_and_engine(fake_registry, state_home, ws, capsys):
    from ctx.cli import main as cli_main

    rc = cli_main(["--workspace", str(ws), "lsp", "refs", "repo:mod.py:hello"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("[ctx lsp refs ") and "engine lsp" in out
    assert "repo:mod.py:L1: def hello(name):" in out and "repo:other.py:L1: hello" in out

    rc = cli_main(["--workspace", str(ws), "lsp", "hover", "mod.py:1:5"])
    out = capsys.readouterr().out
    assert rc == 0 and "def hello(name: str) -> str" in out

    rc = cli_main(["--workspace", str(ws), "lsp", "def", "mod.py:4:7"])
    out = capsys.readouterr().out
    assert rc == 0 and "repo:mod.py:L1" in out


def test_cli_lsp_verb_without_a_server_exits_two(monkeypatch, state_home, ws, capsys):
    from ctx.cli import main as cli_main

    monkeypatch.setenv("CTX_LSP_SERVERS", json.dumps({}))
    rc = cli_main(["--workspace", str(ws), "lsp", "hover", "mod.py:1:5"])
    err = capsys.readouterr().err
    assert rc == 2 and "no language server" in err


@pytest.mark.skipif(shutil.which("pyright-langserver") is None, reason="pyright not on PATH")
def test_real_pyright_answers_references(monkeypatch, state_home, ws):
    monkeypatch.delenv("CTX_LSP_SERVERS", raising=False)
    from ctx.lsp import query

    sites, server = query(ws, "mod.py", 1, 5, "references", timeout=60)
    assert server == "pyright-langserver"
    assert ("mod.py", 1, "def hello(name):") in sites and ("mod.py", 4, "print(hello('x'))") in sites


def test_refs_ladder_uses_the_language_server_rung_and_discloses_it(fake_registry, state_home, ws, capsys):
    from ctx.cli import main as cli_main

    rc = cli_main(["--workspace", str(ws), "refs", "hello"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "engine lsp (" in out.splitlines()[0]
    assert "repo:other.py:L1: hello" in out


def test_refs_ladder_falls_through_when_the_tier_is_off(fake_registry, state_home, ws, capsys, monkeypatch):
    from ctx.cli import main as cli_main

    monkeypatch.setenv("CTX_LSP", "off")
    rc = cli_main(["--workspace", str(ws), "refs", "hello"])
    out = capsys.readouterr().out
    assert rc == 0 and "engine lsp" not in out.splitlines()[0]
