"""ast-grep tier: probe discipline, structural search + labeled fallback,
transactional generation-guarded rewrites.

The binary is absent on the minimal CI job by design — the fallback paths
are the contract; a fake binary on PATH exercises the structural paths.
"""

import json
import os
import stat
import subprocess

import pytest

from conftest import make_store, make_ws


@pytest.fixture()
def git_ws(tmp_path, state_home):
    ws = tmp_path / "proj"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / "m.py").write_text(
        "client = None\n"
        "def go(x):\n"
        "    return old_client.fetch(x)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    return ws


def _fake_astgrep(tmp_path, monkeypatch, script_body: str):
    """Install a fake ast-grep on PATH; caller must cache_clear around it."""
    from ctx import astgrep

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "ast-grep"
    exe.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    astgrep.binary.cache_clear()
    return exe


def test_probe_rejects_shadow_utils_sg(tmp_path, monkeypatch):
    """A PATH `sg` that is not ast-grep (shadow-utils ships one) must not
    be trusted — the probe requires 'ast-grep' in --version output."""
    from ctx import astgrep

    bindir = tmp_path / "bin"
    bindir.mkdir()
    sg = bindir / "sg"
    sg.write_text("#!/bin/sh\necho 'Usage: sg group [[-c] command]'\nexit 0\n")
    sg.chmod(sg.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir))
    astgrep.binary.cache_clear()
    astgrep.lib_available.cache_clear()
    try:
        assert astgrep.binary() is None
        # Binary absent: identity is the library rung if present, else regex.
        expected = (
            f"ast-grep-py {astgrep._lib_version()}"
            if astgrep.lib_available()
            else "regex-fallback"
        )
        assert astgrep.engine_id() == expected
    finally:
        astgrep.binary.cache_clear()
        astgrep.lib_available.cache_clear()


def test_fallback_regex_derivation():
    from ctx.astgrep import _fallback_regex
    import re

    rx = re.compile(_fallback_regex("$CLIENT.authorize($ARG)"))
    assert rx.search("svc.authorize(request.user)")
    assert not rx.search("authorize_all()")
    rx3 = re.compile(_fallback_regex("f($$$ARGS)"))
    assert rx3.search("f(1, 2, 3)") and rx3.search("f()")


def test_ast_search_fallback_is_labeled_and_sorted(git_ws):
    from ctx import astgrep

    astgrep.binary.cache_clear()
    astgrep.lib_available.cache_clear()
    ws = make_ws(git_ws)
    store = make_store(ws)
    # This is the bottom-rung shape: it only holds when neither the binary
    # nor the ast-grep-py library is present to intercept first.
    if astgrep.available():  # environment has a real binary: not this test's shape
        pytest.skip("real ast-grep present")
    if astgrep.lib_available():  # library rung would intercept before regex
        pytest.skip("ast-grep-py library present (library rung, not regex)")
    rows, meta = astgrep.ast_search(ws, store, "old_client.fetch($X)")
    assert meta["engine"] == "regex-fallback"
    assert "textual" in meta["precision"]
    assert [r["file"] for r in rows] == ["m.py"]
    assert rows[0]["line"] == 3


_FAKE_SEARCH = """
import json, sys
args = sys.argv[1:]
if "--version" in args:
    print("ast-grep 9.9.9-test"); raise SystemExit(0)
# emit two matches out of order to prove the caller sorts
print(json.dumps({"file": "z.py", "range": {"start": {"line": 4, "column": 0}}, "lines": "old_client.fetch(b)"}))
print(json.dumps({"file": "m.py", "range": {"start": {"line": 2, "column": 11}}, "lines": "old_client.fetch(x)"}))
"""


def test_ast_search_with_fake_binary(git_ws, tmp_path, monkeypatch):
    from ctx import astgrep

    _fake_astgrep(tmp_path, monkeypatch, _FAKE_SEARCH)
    try:
        ws = make_ws(git_ws)
        store = make_store(ws)
        rows, meta = astgrep.ast_search(ws, store, "old_client.fetch($X)")
        assert meta["engine"] == "ast-grep 9.9.9-test"
        assert meta["precision"] == "structural"
        # 0-based ast-grep lines become 1-based; results sorted by path.
        assert [(r["file"], r["line"]) for r in rows] == [("m.py", 3), ("z.py", 5)]
    finally:
        astgrep.binary.cache_clear()


_FAKE_REWRITE = """
import json, sys, pathlib
args = sys.argv[1:]
if "--version" in args:
    print("ast-grep 9.9.9-test"); raise SystemExit(0)
# compute a real byte-offset replacement over m.py
data = pathlib.Path("m.py").read_bytes()
needle = b"old_client.fetch(x)"
start = data.find(needle)
print(json.dumps({
    "file": "m.py",
    "range": {"start": {"line": 2, "column": 11},
              "byteOffset": {"start": start, "end": start + len(needle)}},
    "replacement": "new_client.fetch(resource=x)",
}))
"""


def test_rewrite_preview_and_apply_transactional(git_ws, tmp_path, monkeypatch):
    from ctx import astgrep

    _fake_astgrep(tmp_path, monkeypatch, _FAKE_REWRITE)
    try:
        ws = make_ws(git_ws)
        store = make_store(ws)
        rows, meta = astgrep.rewrite_preview(
            ws, store, "old_client.fetch($X)", "new_client.fetch(resource=$X)"
        )
        assert rows == [{"file": "m.py", "edits": 1}]
        assert meta["patch_blob"] and meta["generation"]
        # Preview never touches the worktree.
        assert "old_client" in (git_ws / "m.py").read_text(encoding="utf-8")

        applied, ameta = astgrep.rewrite_apply(
            ws, store, meta["patch_blob"], meta["generation"]
        )
        assert ameta["applied_files"] == 1
        assert "new_client.fetch(resource=x)" in (git_ws / "m.py").read_text(
            encoding="utf-8"
        )
    finally:
        astgrep.binary.cache_clear()


def test_rewrite_apply_refuses_on_generation_drift(git_ws, tmp_path, monkeypatch):
    from ctx import astgrep

    _fake_astgrep(tmp_path, monkeypatch, _FAKE_REWRITE)
    try:
        ws = make_ws(git_ws)
        store = make_store(ws)
        _rows, meta = astgrep.rewrite_preview(
            ws, store, "old_client.fetch($X)", "new_client.fetch(resource=$X)"
        )
        # Drift: any worktree edit after preview invalidates the patch.
        (git_ws / "extra.py").write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(astgrep.RewriteError, match="generation changed"):
            astgrep.rewrite_apply(ws, store, meta["patch_blob"], meta["generation"])
        # Refusal is transactional: nothing was touched.
        assert "old_client" in (git_ws / "m.py").read_text(encoding="utf-8")
    finally:
        astgrep.binary.cache_clear()


def test_rewrite_without_engine_declines(git_ws, monkeypatch):
    from ctx import astgrep

    monkeypatch.setenv("PATH", "/nonexistent")
    astgrep.binary.cache_clear()
    try:
        ws = make_ws(git_ws)
        store = make_store(ws)
        with pytest.raises(astgrep.EngineMissing):
            astgrep.rewrite_preview(ws, store, "a($X)", "b($X)")
    finally:
        astgrep.binary.cache_clear()


def test_ast_search_lib_rung_is_structural_and_sorted(git_ws, monkeypatch):
    """Middle rung: with the binary off PATH but the ast-grep-py library
    present, ast.search yields structural rows via the in-process engine —
    disclosed as ast-grep-py, precision structural, sorted (file, line, col)."""
    pytest.importorskip("ast_grep_py")
    from ctx import astgrep

    # A second file proves the caller path-sorts the library's output.
    (git_ws / "z.py").write_text(
        "def h(y):\n    return old_client.fetch(y)\n", encoding="utf-8"
    )
    monkeypatch.setenv("PATH", "/nonexistent")
    astgrep.binary.cache_clear()
    astgrep.lib_available.cache_clear()
    try:
        ws = make_ws(git_ws)
        store = make_store(ws)
        assert astgrep.binary() is None  # binary rung is out
        assert astgrep.lib_available() is True
        rows, meta = astgrep.ast_search(ws, store, "old_client.fetch($X)")
        assert meta["engine"].startswith("ast-grep-py")
        assert meta["precision"] == "structural"
        # m.py match is at line 3 (fixture), z.py at line 2; path-sorted.
        assert [(r["file"], r["line"]) for r in rows] == [("m.py", 3), ("z.py", 2)]
        assert rows == sorted(rows, key=lambda r: (r["file"], r["line"], r["col"]))
    finally:
        astgrep.binary.cache_clear()
        astgrep.lib_available.cache_clear()


def test_engine_id_precedence_binary_then_lib_then_regex(git_ws, tmp_path, monkeypatch):
    """engine_id() participates in plan_exec cache keys — its precedence is
    binary id > library id > regex-fallback. Exercise all three rungs."""
    from ctx import astgrep

    # Binary rung wins when a (fake) ast-grep is on PATH.
    _fake_astgrep(tmp_path, monkeypatch, _FAKE_SEARCH)
    astgrep.lib_available.cache_clear()
    try:
        assert astgrep.engine_id() == "ast-grep 9.9.9-test"
    finally:
        astgrep.binary.cache_clear()
        astgrep.lib_available.cache_clear()

    # Binary absent: library id when importable, else regex-fallback.
    monkeypatch.setenv("PATH", "/nonexistent")
    astgrep.binary.cache_clear()
    astgrep.lib_available.cache_clear()
    try:
        eid = astgrep.engine_id()
        if astgrep.lib_available():
            assert eid.startswith("ast-grep-py ")
        else:
            assert eid == "regex-fallback"
    finally:
        astgrep.binary.cache_clear()
        astgrep.lib_available.cache_clear()


def test_plan_op_ast_search_discloses_engine(git_ws, tmp_path, monkeypatch):
    """Through the plan executor: the node's coverage line carries the
    fake engine id and version (disclosure + cache-key participation)."""
    from ctx import astgrep
    from ctx.plan_exec import execute_plan

    _fake_astgrep(tmp_path, monkeypatch, _FAKE_SEARCH)
    try:
        ws = make_ws(git_ws)
        store = make_store(ws)
        plan = {
            "version": "ctx.plan/v1",
            "objective": {"kind": "survey", "question": "call sites?"},
            "budget": {"wall_seconds": 60},
            "steps": [
                {"id": "calls", "op": "ast.search",
                 "args": {"pattern": "old_client.fetch($X)"}},
            ],
        }
        text, code = execute_plan(ws, store, plan)
        assert code == 0
        assert "engine ast-grep 9.9.9-test" in text
    finally:
        astgrep.binary.cache_clear()


def test_ctx_rewrite_verb_previews_then_applies(git_ws, tmp_path, monkeypatch):
    """The `ctx rewrite` verb collapses find-and-edit into one op: preview by
    default (worktree untouched), --apply writes transactionally."""
    from argparse import Namespace

    from ctx import astgrep, cli

    _fake_astgrep(tmp_path, monkeypatch, _FAKE_REWRITE)
    try:
        ws = make_ws(git_ws)
        ns = Namespace(pattern="old_client.fetch($X)",
                       replacement="new_client.fetch(resource=$X)",
                       lang=None, glob=None, apply=False)
        assert cli._cmd_rewrite(ws, ns) == 0
        assert "old_client" in (git_ws / "m.py").read_text(encoding="utf-8")  # preview only

        ns.apply = True
        assert cli._cmd_rewrite(ws, ns) == 0
        assert "new_client.fetch(resource=x)" in (git_ws / "m.py").read_text(encoding="utf-8")
    finally:
        astgrep.binary.cache_clear()
