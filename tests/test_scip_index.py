"""Acceptance: generating the index the precise tier reads.

`ctx.scip_ingest` has sat at the top of the refs/def ladders as the exact,
compiler-backed tier since M-K4 — but only "when present", and nothing in ctx
ever made one. This module closes that, by shelling out to the language's own
tooling rather than hand-rolling a better approximation.

The contract under test:

1. **Nothing is written into the repository.** The index lands in the store's
   audit area, for the same reason job spools do.
2. **Absence degrades, never errors.** No indexer installed is an actionable
   message naming what to install, not a traceback, and the ladder keeps its
   existing rungs underneath.
3. **One language table.** `ctx index` and `ctx map` decide what language a
   file is from the same place, so they cannot disagree.
"""

from __future__ import annotations

import pytest

from conftest import make_store, make_ws


def _ws(workspace_dir):
    (workspace_dir / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (workspace_dir / "b.go").write_text("package main\n\nfunc g() {}\n", encoding="utf-8")
    (workspace_dir / "c.go").write_text("package main\n\nfunc h() {}\n", encoding="utf-8")
    return make_ws(workspace_dir)


def test_the_index_never_lands_in_the_worktree(state_home, workspace_dir):
    from ctx import scip_index

    ws = _ws(workspace_dir)
    store = make_store(ws)
    path = scip_index.index_path(store)
    assert ws.root not in path.parents, f"{path} is inside the repository"


def test_a_missing_indexer_names_what_to_install(state_home, workspace_dir, monkeypatch):
    from ctx import scip_index

    ws = _ws(workspace_dir)
    store = make_store(ws)
    monkeypatch.setattr(scip_index.shutil, "which", lambda _b: None)

    with pytest.raises(scip_index.IndexError_) as e:
        scip_index.build(ws, store, language="go")
    msg = str(e.value)
    assert "no SCIP indexer for go" in msg
    assert "scip-go" in msg, "the error must say what to install"


def test_the_roster_reports_what_is_actually_runnable(state_home, monkeypatch):
    from ctx import scip_index

    monkeypatch.setattr(scip_index.shutil, "which", lambda b: "/usr/bin/" + b
                        if b == "scip-go" else None)
    roster = scip_index.roster()
    assert roster["go"] == "scip-go"
    assert roster["rust"] is None


def test_languages_come_from_the_skeletons_table_not_a_second_one(
    state_home, workspace_dir
):
    """`ctx index` and `ctx map` must agree about what a file is."""
    from ctx import scip_index

    ws = _ws(workspace_dir)
    langs = scip_index.dominant_languages(ws)
    assert langs[0] == "go", "two .go files should outrank one .py"
    assert set(langs) == {"go", "python"}


def test_a_workspace_with_nothing_indexable_is_empty_not_an_error(
    state_home, workspace_dir
):
    from ctx import scip_index

    (workspace_dir / "notes.md").write_text("# hi\n", encoding="utf-8")
    ws = make_ws(workspace_dir)
    assert scip_index.dominant_languages(ws) == []


def test_an_indexer_that_produces_nothing_is_reported_with_its_own_words(
    state_home, workspace_dir, monkeypatch
):
    """A silent failure here would send every later answer to the regex floor
    without saying why — the exact shape of defect this work exists to close."""
    import subprocess

    from ctx import scip_index

    ws = _ws(workspace_dir)
    store = make_store(ws)
    monkeypatch.setattr(scip_index.shutil, "which", lambda _b: "/usr/bin/scip-go")

    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="go.mod not found\n"
        )

    monkeypatch.setattr(scip_index.subprocess, "run", fake_run)
    with pytest.raises(scip_index.IndexError_) as e:
        scip_index.build(ws, store, language="go")
    assert "go.mod not found" in str(e.value)


def test_index_list_reports_the_workspace_and_the_tools(
    state_home, workspace_dir, capsys
):
    from ctx.cli import main

    ws = _ws(workspace_dir)
    assert main(["--workspace", str(ws.root), "index", "--list"]) == 0
    out = capsys.readouterr().out
    assert "available indexers" in out
    assert "go" in out and "rust" in out
    assert "in this workspace:" in out
