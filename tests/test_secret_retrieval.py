"""Retrieval refuses the guard's credential files: one list (ctx.secretpaths),
any letter case, backup suffixes, template copies exempt."""

from __future__ import annotations

import pytest

from conftest import make_store, make_ws


def test_retrieval_refuses_credential_files_in_any_case(tmp_path, monkeypatch):
    from ctx.retrieval import Selector, get, search

    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "ws"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n")
    (root / ".NPMRC").write_text("//r/:_authToken=TOKEN123\n")
    (root / "x.TFVARS").write_text('password = "TOKEN123"\n')
    (root / ".npmrc.bak").write_text("//r/:_authToken=TOKEN123\n")
    (root / ".npmrc.example").write_text("registry=TOKEN-free\n")
    ws = make_ws(root)
    store = make_store(ws)
    for ref in ("repo:.NPMRC", "repo:x.TFVARS", "repo:.npmrc.bak"):
        with pytest.raises(Exception):
            get(store, ws, ref, Selector())
    for kw in ({"glob": "*.TFVARS"}, {}):
        out = search(store, ws, "repo:.", ["TOKEN123"], **kw)
        assert ".NPMRC" not in out and "x.TFVARS" not in out and ".npmrc.bak" not in out, out
    assert "TOKEN-free" in get(store, ws, "repo:.npmrc.example", Selector())


def test_one_list_for_the_guard_and_retrieval():
    from ctx import hook
    from ctx.secretpaths import is_credential_file

    for name in (".npmrc", ".NPMRC", ".netrc~", "a/b/terraform.TFVARS", ".pypirc.old"):
        assert is_credential_file(name) and hook._is_secret_path(name, "/nonexistent"), name
    for name in (".npmrc.example", ".npmrc.sample", "terraform.tfvars.example", ".pypirc.sample"):
        assert not is_credential_file(name) and not hook._is_secret_path(name, "/nonexistent")
