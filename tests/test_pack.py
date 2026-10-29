"""`ctx pack`: a ranked, budgeted, evidence-carrying context pack for a
task, from the text index (terms verified against the bytes, symbols,
paths, history) — deterministic for a tree state, and honest about why each
file is there."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from conftest import make_store, make_ws

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "a@x",
        "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "a@x",
        "GIT_AUTHOR_DATE": "2026-01-02T00:00:00", "GIT_COMMITTER_DATE": "2026-01-02T00:00:00"}


def _git(root, *argv):
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True, env=_ENV)


@pytest.fixture()
def repo(git_workspace):
    root = git_workspace
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "converters.py").write_text(
        "class Converter:\n    def structure(self, obj, cl):\n        return self._structure(obj, cl)\n\n"
        "    def _structure(self, obj, cl):\n        return cl(**obj)\n\n"
        "def structure_attrs(obj, cl):\n    return Converter().structure(obj, cl)\n",
        encoding="utf-8",
    )
    (root / "src" / "cols.py").write_text("def list_structure(items):\n    return list(items)\n",
                                          encoding="utf-8")
    (root / "src" / "unrelated.py").write_text("def nothing():\n    return 0\n", encoding="utf-8")
    (root / "tests" / "test_converters.py").write_text(
        "from src.converters import Converter, structure_attrs\n\ndef test_structure():\n"
        "    assert structure_attrs({}, dict) == {}\n", encoding="utf-8")
    (root / "docs" / "structuring.md").write_text(
        "# Structuring\nThe Converter structures objects. structure structure structure.\n",
        encoding="utf-8")
    (root / "README.md").write_text("A converter library.\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "converters: add partial structure recovery groundwork")
    return root


def test_terms_are_weighted_by_where_they_come_from():
    from ctx.pack import extract_terms

    terms, paths = extract_terms(
        "Add `partial_structure` to `BaseConverter` so that structure_attrs in src/cattrs/converters.py "
        "returns a PartialResult; the value should be None when required fields fail."
    )
    by = {t.key: t for t in terms}
    assert by["partial_structure"].weight >= 2.0          # backticked span
    assert by["baseconverter"].weight >= 2.0
    assert by["partialresult"].weight >= 1.5               # code-shaped
    assert by["structure"].key in by and by["structure"].weight < by["partial_structure"].weight
    assert "should" not in by and "the" not in by           # stopwords
    assert paths == ["src/cattrs/converters.py"]


def test_pack_ranks_the_defining_file_first_and_says_why(state_home, repo):
    from ctx.pack import build_pack, render_pack

    ws = make_ws(repo)
    store = make_store(ws)
    pack = build_pack(store, ws, "Add `partial_structure` to `Converter` next to structure_attrs "
                                 "so a failed structure returns a partial result")
    files = [r.rel for r in pack.rows]
    assert files[0] == "src/converters.py", files
    top = pack.rows[0]
    assert any(n == "Converter" for n, _k, _l in top.symbols)
    assert "structure_attrs" in top.terms or "Converter" in top.terms
    assert any(sha for sha, _s in top.commits), "the commit about structuring is evidence"
    # tests and prose rank below the source they describe
    assert files.index("src/converters.py") < files.index("tests/test_converters.py")
    assert files.index("src/converters.py") < files.index("docs/structuring.md")
    out = render_pack(pack)
    assert out.startswith("[ctx pack · ") and "1. repo:src/converters.py" in out
    assert "why: " in out and "defines Converter" in out and "commit " in out
    assert "class Converter L1" in out and "next:" in out


def test_pack_is_deterministic_and_verified_not_trusted(state_home, repo):
    """A file whose trigrams match a term but whose text does not contain
    it as a word is not a hit."""
    from ctx.pack import build_pack

    (repo / "src" / "decoy.py").write_text("x = 'converterstructure_attrsx'\n", encoding="utf-8")
    ws = make_ws(repo)
    store = make_store(ws)
    a = build_pack(store, ws, "structure_attrs Converter", history=False)
    b = build_pack(store, ws, "structure_attrs Converter", history=False)
    assert [r.rel for r in a.rows] == [r.rel for r in b.rows]
    assert "src/decoy.py" not in [r.rel for r in a.rows]


def test_pack_sees_a_file_added_after_the_first_pack(state_home, repo):
    from ctx.pack import build_pack

    ws = make_ws(repo)
    store = make_store(ws)
    first = build_pack(store, ws, "where is the FrobnicateWidget defined", history=False)
    assert not any(r.rel == "src/new.py" for r in first.rows)
    (repo / "src" / "new.py").write_text("class FrobnicateWidget:\n    pass\n", encoding="utf-8")
    second = build_pack(store, ws, "where is the FrobnicateWidget defined", history=False)
    assert second.rows and second.rows[0].rel == "src/new.py"


def test_pack_budget_bounds_the_rendering(state_home, repo):
    from ctx.pack import build_pack, render_pack

    ws = make_ws(repo)
    store = make_store(ws)
    pack = build_pack(store, ws, "structure Converter structure_attrs list_structure nothing",
                      budget_tokens=60, history=False)
    out = render_pack(pack)
    assert "more files within the candidates" in out or len(pack.rows) <= 3


def test_pack_cli_text_json_and_file_input(state_home, repo, capsys):
    from ctx.cli import main

    (repo / "task.md").write_text("Add partial structure recovery to `Converter`\n", encoding="utf-8")
    rc = main(["--workspace", str(repo), "pack", "@task.md", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    doc = json.loads(out)
    assert doc["schema"] == "ctx.pack/v1" and doc["files"][0]["file"] == "src/converters.py"
    assert doc["files"][0]["symbols"] and doc["terms"]
    rc = main(["--workspace", str(repo), "pack", "partial structure recovery in Converter", "--files", "2"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("[ctx pack · 2 of ")
    rc = main(["--workspace", str(repo), "pack", "   "])
    assert rc == 2


def test_pack_refuses_when_the_index_is_off(state_home, repo, monkeypatch, capsys):
    from ctx.cli import main

    monkeypatch.setenv("CTX_SEARCH_INDEX", "off")
    rc = main(["--workspace", str(repo), "pack", "anything"])
    assert rc == 2 and "index is off" in capsys.readouterr().err


def test_pack_bundles_source_and_scoped_relationships(state_home, repo):
    from ctx.pack import build_pack, render_pack

    ws = make_ws(repo)
    pack = build_pack(make_store(ws), ws, "structure_attrs", history=False)
    row = next(r for r in pack.rows if r.rel == "src/converters.py")
    assert "def structure_attrs" in row.context and "snapshot:" in row.context
    assert any(r["direction"] == "caller" and r["file"] == "tests/test_converters.py"
               for r in row.relationships)
    assert all(r["tier"] != "repo" for r in row.relationships)
    rendered = render_pack(pack)
    assert "caller:" in rendered and "snapshot:" in rendered
    assert "scoped static callers/callees" in rendered


def test_pack_context_snapshot_survives_source_edit(state_home, repo):
    from ctx.pack import build_pack
    from ctx.retrieval import Selector, get
    import re

    ws = make_ws(repo)
    store = make_store(ws)
    pack = build_pack(store, ws, "structure_attrs", history=False)
    row = next(r for r in pack.rows if r.rel == "src/converters.py")
    snapshot = re.search(r"snapshot:([a-f0-9]+)", row.context).group(0)
    (repo / row.rel).write_text("# changed after discovery\n")
    recovered = get(store, ws, snapshot, Selector(lines=(1, 20)))
    assert "def structure_attrs" in recovered


def test_relationships_do_not_merge_same_named_functions(state_home, repo):
    from ctx.callgraph import cmd_callers, context_neighbors

    (repo / "a.py").write_text("def shared():\n    return 1\ndef caller_a():\n    return shared()\n")
    (repo / "b.py").write_text("def shared():\n    return 2\ndef caller_b():\n    return shared()\n")
    ws = make_ws(repo)
    result = context_neighbors(make_store(ws), ws, [("a.py", 1)])
    callers = result[("a.py", 1)]["rows"]
    assert [r["symbol"] for r in callers] == ["caller_a"]
    scoped = cmd_callers(make_store(ws), ws, "a.py:shared")
    assert "caller_a" in scoped and "caller_b" not in scoped


@pytest.mark.parametrize("budget", [0, 1, 60, 300, 2500])
def test_pack_respects_budget_with_source_and_many_relationships(state_home, repo, budget):
    from ctx.pack import build_pack, render_pack

    (repo / "many.py").write_text(
        "from src.converters import structure_attrs\n" +
        "\n".join(f"def caller_{i}():\n    return structure_attrs({{}}, dict)" for i in range(20)))
    ws = make_ws(repo)
    pack = build_pack(make_store(ws), ws, "structure_attrs", budget_tokens=budget, history=False)
    out = render_pack(pack)
    assert len(out) <= budget * 4
    if budget >= 60:
        assert "next:" in out


def test_omitted_callees_have_a_scoped_continuation(state_home, repo):
    from ctx.pack import build_pack, render_pack

    (repo / "entry.py").write_text(
        "def entry_point():\n" + "".join(f"    helper_{i}()\n" for i in range(10)) +
        "\n".join(f"def helper_{i}():\n    return {i}\n" for i in range(10)))
    ws = make_ws(repo)
    pack = build_pack(make_store(ws), ws, "entry_point", history=False)
    row = next(r for r in pack.rows if r.rel == "entry.py")
    assert row.relationships_omitted == 6
    rendered = render_pack(pack)
    assert "ctx callees entry.py:entry_point" in rendered
