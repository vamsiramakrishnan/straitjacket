"""Acceptance: M-K2 file-set algebra (docs/SUBSTRATE.md §4) — the
``file_select`` operator class. Engine ladder git → fd → walk with
byte-identical listings (parity by construction: one ignore filter, one
terminal sort), coverage receipts on every emission, ``--changed`` bound
to generation facts (never mtime), and the ledger-dir exclusion."""

from __future__ import annotations

import shutil

import pytest

from conftest import make_store, make_ws

HAS_FD = any(shutil.which(n) for n in ("fd", "fdfind"))


def _seed(root):
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "util.js").write_text("// x\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "a.md").write_text("# a\n", encoding="utf-8")
    (root / ".ctx-session-reads").mkdir(exist_ok=True)
    (root / ".ctx-session-reads" / "ledger.json").write_text("{}", encoding="utf-8")


# ------------------------------------------------------------ module layer
def test_walk_engine_lists_sorted_and_excludes_ledger(state_home, workspace_dir):
    from ctx import filesets

    ws = make_ws(workspace_dir)
    _seed(workspace_dir)
    rows, coverage, omitted = filesets.select(ws)
    files = [r["file"] for r in rows]
    assert files == sorted(files)
    assert "src/app.py" in files and "docs/a.md" in files
    assert not any(f.startswith(".ctx-session-reads") for f in files)
    assert coverage["engine"] in ("git", "walk")
    assert coverage["considered"] >= coverage["selected"] == len(files) + 0
    assert omitted == 0


def test_ext_glob_exclude_and_max(state_home, workspace_dir):
    from ctx import filesets

    ws = make_ws(workspace_dir)
    _seed(workspace_dir)
    rows, cov, _ = filesets.select(ws, exts=["py"])
    assert [r["file"] for r in rows] == ["src/app.py"]
    rows, cov, _ = filesets.select(ws, globs=["src/*"])
    assert {r["file"] for r in rows} == {"src/app.py", "src/util.js"}
    rows, cov, _ = filesets.select(ws, globs=["src/*"], excludes=["**/*.js"])
    assert [r["file"] for r in rows] == ["src/app.py"]
    rows, cov, omitted = filesets.select(ws, max_files=1)
    assert len(rows) == 1 and omitted == cov["selected"] - 1 > 0


def test_python_killswitch_forces_stdlib(state_home, workspace_dir, monkeypatch):
    from ctx import filesets

    ws = make_ws(workspace_dir)
    _seed(workspace_dir)
    monkeypatch.setenv("CTX_FILES_ENGINE", "python")
    rels, engine = filesets.enumerate_files(ws)
    assert engine in ("git", "walk")  # never fd under the kill-switch


@pytest.mark.skipif(not HAS_FD, reason="fd not installed")
def test_fd_and_walk_engines_agree(state_home, workspace_dir, monkeypatch):
    """The parity gate: fd is a faster walk, nothing more — identical
    sorted listings, because ignore filtering happens in exactly one
    place (ws.is_ignored) for every engine."""
    from ctx import filesets

    ws = make_ws(workspace_dir)
    _seed(workspace_dir)
    (workspace_dir / ".hidden.cfg").write_text("h\n", encoding="utf-8")
    monkeypatch.setenv("CTX_FILES_ENGINE", "fd")
    fd_rels, fd_engine = filesets.enumerate_files(ws)
    monkeypatch.setenv("CTX_FILES_ENGINE", "python")
    py_rels, _ = filesets.enumerate_files(ws)
    assert fd_engine == "fd"
    assert fd_rels == py_rels


def test_forced_fd_without_binary_degrades_labeled(state_home, workspace_dir, monkeypatch):
    from ctx import filesets

    ws = make_ws(workspace_dir)
    _seed(workspace_dir)
    monkeypatch.setenv("CTX_FILES_ENGINE", "fd")
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    rels, engine = filesets.enumerate_files(ws)
    assert engine in ("git", "walk") and rels  # degrades, never errors


def test_changed_binds_to_generation_facts(state_home, git_workspace):
    """--changed = the porcelain snapshot (generation facts), so a clean
    tree selects nothing and an edit selects exactly the edited file —
    independent of any file's mtime."""
    from ctx import filesets
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(git_workspace))
    rows, cov, _ = filesets.select(ws, changed=True)
    assert rows == []  # clean tree: no changed facts, regardless of mtime
    (git_workspace / "hello.py").write_text("print('changed')\n", encoding="utf-8")
    (git_workspace / "new.py").write_text("y = 2\n", encoding="utf-8")
    rows, cov, _ = filesets.select(ws, changed=True)
    assert {r["file"] for r in rows} == {"hello.py", "new.py"}
    rows, cov, _ = filesets.select(ws, changed=True, exts=["py"], globs=["new*"])
    assert [r["file"] for r in rows] == ["new.py"]


def test_changed_in_non_git_workspace_is_empty_not_error(state_home, workspace_dir):
    from ctx import filesets

    ws = make_ws(workspace_dir)
    _seed(workspace_dir)
    rows, cov, _ = filesets.select(ws, changed=True)
    assert rows == [] and cov.get("generation") is None


def test_determinism_byte_identical(state_home, workspace_dir):
    from ctx import filesets

    ws = make_ws(workspace_dir)
    _seed(workspace_dir)
    a = filesets.select(ws, exts=["py", "md"])
    b = filesets.select(ws, exts=["py", "md"])
    assert a == b


# ------------------------------------------------------------ corpus stage
def test_corpus_stage_rows_coverage_and_render(state_home, workspace_dir):
    from ctx.query import run_query

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed(workspace_dir)
    out, code = run_query(ws, store, "corpus --ext py")
    assert code == 0
    assert "src/app.py" in out
    assert "coverage: considered" in out and "selected 1" in out
    assert "engine" in out
    # files-kind rows without a sites count render size, not "0 sites".
    assert "0 sites" not in out


def test_corpus_pipes_into_outline_and_combinators(state_home, workspace_dir):
    from ctx.query import run_query

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed(workspace_dir)
    out, code = run_query(ws, store, "corpus --glob 'src/*' | where file~.py | count")
    assert code == 0 and "n=1" in out
    # Coverage receipt survives the combinators (executor carry-forward).
    assert "coverage: considered" in out


def test_corpus_changed_empty_teaches(state_home, workspace_dir):
    from ctx.query import run_query

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed(workspace_dir)
    out, code = run_query(ws, store, "corpus --changed")
    assert code == 0
    assert "0 rows after stage 1" in out and "generation" in out


# ------------------------------------------------------------ plan op tier
def test_repo_files_plan_op_payload(state_home, workspace_dir):
    from ctx.plan_ops import OPS, PlanContext

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed(workspace_dir)
    spec = OPS["repo.files"]
    assert spec.klass == "observe" and spec.cost == "index"
    pc = PlanContext(ws=ws, store=store)
    out = spec.fn(pc, {"ext": "py"}, None)
    assert out["kind"] == "files"
    assert [r["file"] for r in out["rows"]] == ["src/app.py"]
    assert out["meta"]["selected"] == 1 and out["meta"]["considered"] >= 1
    # List-valued args accepted too (plan JSON convenience).
    out2 = spec.fn(pc, {"ext": ["py", "md"], "exclude": "docs/*"}, None)
    assert [r["file"] for r in out2["rows"]] == ["src/app.py"]
