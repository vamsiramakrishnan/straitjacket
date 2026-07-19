"""Semgrep tier: hermetic invocation, normalized/sorted findings, taint
traces, declared skip on absence, path narrowing from an input node.

Semgrep is absent on the minimal CI job by design; a fake binary on PATH
exercises the live paths and asserts the hermetic flags are actually
passed (the fake refuses to answer without them).
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
    (ws / "app.py").write_text("import os\n", encoding="utf-8")
    (ws / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    return ws


_FAKE_SEMGREP = """
import json, sys
args = sys.argv[1:]
if "--version" in args:
    print("1.99.0-test"); raise SystemExit(0)
# Hermeticity is not advisory: refuse to run without the flags.
if "--metrics=off" not in args or "--disable-version-check" not in args:
    sys.stderr.write("fake-semgrep: missing hermetic flags\\n"); raise SystemExit(7)
findings = {
    "results": [
        {"check_id": "auth-taint", "path": "z.py", "start": {"line": 9},
         "extra": {"message": "sink reached"}},
        {"check_id": "auth-taint", "path": "app.py", "start": {"line": 3},
         "extra": {"message": "untrusted header reaches authorize",
                   "dataflow_trace": {"taint_source": [
                       {"path": "app.py", "start": {"line": 1}},
                       {"path": "app.py", "start": {"line": 3}}]}}},
    ],
    "errors": [],
}
print(json.dumps(findings))
"""


def _fake_semgrep(tmp_path, monkeypatch):
    from ctx import semgrep_engine

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "semgrep"
    exe.write_text("#!/usr/bin/env python3\n" + _FAKE_SEMGREP, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    semgrep_engine.binary.cache_clear()
    return exe


def test_absent_engine_raises_engine_missing(git_ws, monkeypatch):
    from ctx import semgrep_engine

    monkeypatch.setenv("PATH", "/nonexistent")
    semgrep_engine.binary.cache_clear()
    try:
        ws = make_ws(git_ws)
        with pytest.raises(semgrep_engine.EngineMissing):
            semgrep_engine.scan(ws, "rules.yaml")
    finally:
        semgrep_engine.binary.cache_clear()


def test_scan_normalizes_sorts_and_discloses(git_ws, tmp_path, monkeypatch):
    from ctx import semgrep_engine

    _fake_semgrep(tmp_path, monkeypatch)
    try:
        ws = make_ws(git_ws)
        rows, meta = semgrep_engine.scan(ws, "rules.yaml")
        assert meta["engine"] == "semgrep 1.99.0-test"
        assert [r["file"] for r in rows] == ["app.py", "z.py"]  # sorted
        taint = rows[0]
        assert taint["rule"] == "auth-taint"
        assert taint["trace"] == ["app.py:1", "app.py:3"]  # frames, in order
    finally:
        semgrep_engine.binary.cache_clear()


def test_rules_file_must_be_inside_workspace(git_ws, tmp_path, monkeypatch):
    from ctx import semgrep_engine
    from ctx.workspace import WorkspaceError

    _fake_semgrep(tmp_path, monkeypatch)
    try:
        ws = make_ws(git_ws)
        with pytest.raises(WorkspaceError):
            semgrep_engine.scan(ws, "../outside-rules.yaml")
    finally:
        semgrep_engine.binary.cache_clear()


def test_plan_semantic_taint_with_input_narrowing(git_ws, tmp_path, monkeypatch):
    """semantic.taint through the executor: an input node narrows the scan
    to its files, taint mode keeps trace-bearing rows first, the engine is
    disclosed in coverage."""
    from ctx import semgrep_engine
    from ctx.plan_exec import execute_plan

    _fake_semgrep(tmp_path, monkeypatch)
    try:
        ws = make_ws(git_ws)
        store = make_store(ws)
        plan = {
            "version": "ctx.plan/v1",
            "objective": {"kind": "diagnose", "question": "does the header reach authorize?"},
            "budget": {"wall_seconds": 60},
            "steps": [
                {"id": "sites", "op": "code.search", "args": {"pattern": "import os"}},
                {"id": "taint", "op": "semantic.taint",
                 "args": {"rules": "rules.yaml"}, "input": "sites"},
            ],
        }
        text, code = execute_plan(ws, store, plan)
        assert code == 0
        assert "engine semgrep 1.99.0-test" in text
        assert "taint · semantic.taint" in text
    finally:
        semgrep_engine.binary.cache_clear()
