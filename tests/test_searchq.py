"""The search query language (ctx.searchq): filters in the pattern list,
and git history as evidence — in `ctx search`, and in the q algebra's
search / commits / touched / history stages."""

from __future__ import annotations

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
    (root / "src" / "limiter.py").write_text(
        "class TokenBucket:\n    def take(self, n):\n        return n\n", encoding="utf-8"
    )
    (root / "tests" / "test_limiter.py").write_text(
        "from src.limiter import TokenBucket\n\ndef test_take():\n    assert TokenBucket().take(1) == 1\n",
        encoding="utf-8",
    )
    (root / "notes.md").write_text("TokenBucket notes\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add the token bucket limiter")
    (root / "src" / "limiter.py").write_text(
        "class TokenBucket:\n    def take(self, n):\n        return max(0, n)\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "limiter: clamp negative takes")
    return root


# ------------------------------------------------------------- parsing
def test_parse_splits_filters_from_patterns():
    from ctx.searchq import parse

    q = parse(["TokenBucket", "file:src/", "!file:tests", "lang:py", "case:no", "sym:take",
               "after:2026-01-01", "author:Ada", "re:kept"])
    assert q.patterns == ["TokenBucket", "re:kept"]
    assert q.files == ["src/"] and q.not_files == ["tests"] and q.langs == {"python"}
    assert q.case is False and q.syms == ["take"] and q.after == "2026-01-01" and q.author == "Ada"
    assert q.kind == "code" and q.has_filters
    assert "file:src/" in q.describe() and "-file:tests" in q.describe()


def test_parse_rejects_bad_values():
    from ctx.searchq import QueryError, parse

    with pytest.raises(QueryError):
        parse(["lang:cobol"])
    with pytest.raises(QueryError):
        parse(["type:branch"])
    with pytest.raises(QueryError):
        parse(["case:maybe"])


def test_keep_path_applies_globs_regexes_and_languages():
    from ctx.searchq import keep_path, parse

    q = parse(["x", "file:src/", "!file:*_test.py", "lang:python"])
    assert keep_path("src/limiter.py", q)
    assert not keep_path("tests/test_limiter.py", q)
    assert not keep_path("src/notes.md", q)
    assert not keep_path("src/a_test.py", q)


# -------------------------------------------------------------- search
def test_search_filters_narrow_the_corpus(state_home, repo, monkeypatch):
    from ctx.retrieval import search

    monkeypatch.delenv("CTX_SEARCH_ENGINE", raising=False)
    ws = make_ws(repo)
    store = make_store(ws)
    out = search(store, ws, "repo:", ["TokenBucket", "!file:tests", "lang:python"])
    assert "src/limiter.py:" in out and "tests/test_limiter.py" not in out and "notes.md" not in out
    assert "-file:tests lang:python" in out.splitlines()[1]
    out = search(store, ws, "repo:", ["tokenbucket", "case:no", "file:notes"])
    assert "notes.md:" in out and "src/limiter.py" not in out


def test_sym_alone_answers_definition_sites(state_home, repo, monkeypatch):
    from ctx.retrieval import search

    monkeypatch.delenv("CTX_SEARCH_ENGINE", raising=False)
    ws = make_ws(repo)
    store = make_store(ws)
    out = search(store, ws, "repo:", ["sym:TokenBucket"])
    assert "src/limiter.py:" in out and "L1: class TokenBucket:" in out
    assert "symbol table" in out and "tests/test_limiter.py" not in out
    # sym: with a pattern restricts the corpus to the defining files
    out = search(store, ws, "repo:", ["take", "sym:TokenBucket"])
    assert "src/limiter.py:" in out and "tests/test_limiter.py" not in out


def test_filters_alone_are_an_error(state_home, repo, monkeypatch):
    from ctx.retrieval import RetrievalError, search

    ws = make_ws(repo)
    with pytest.raises(RetrievalError, match="filters alone"):
        search(make_store(ws), ws, "repo:", ["file:src/"])


def test_history_search_returns_commits_as_evidence(state_home, repo):
    from ctx.retrieval import search

    ws = make_ws(repo)
    store = make_store(ws)
    out = search(store, ws, "repo:", ["type:commit", "clamp"])
    lines = out.splitlines()
    assert lines[0] == "[ctx search repo: · history]"
    assert "limiter: clamp negative takes" in out and "add the token bucket" not in out
    assert "files: src/limiter.py" in out and "commits: 1" in out
    assert "result: blob:" in out and "git show --stat" in out
    # type:diff finds the commit whose change carries the text
    out = search(store, ws, "repo:", ["type:diff", "max\\(0", "file:src/limiter.py"])
    assert "limiter: clamp negative takes" in out and "commits: 1" in out
    # author and date filters map to git's own
    out = search(store, ws, "repo:", ["type:commit", "limiter", "author:Nobody"])
    assert "commits: 0" in out


def test_history_search_outside_git_is_a_clean_error(state_home, workspace_dir):
    from ctx.retrieval import RetrievalError, search

    ws = make_ws(workspace_dir)
    with pytest.raises(RetrievalError, match="git workspace"):
        search(make_store(ws), ws, "repo:", ["type:commit", "x"])


def test_search_cli_accepts_the_bang_spelling(state_home, repo, capsys):
    from ctx.cli import main

    rc = main(["--workspace", str(repo), "search", "repo:", "TokenBucket", "!file:tests"])
    out = capsys.readouterr().out
    assert rc == 0 and "src/limiter.py:" in out and "test_limiter" not in out


# --------------------------------------------------------------- q stages
def test_q_search_stage_takes_filters(state_home, repo, capsys):
    from ctx.cli import main

    rc = main(["--workspace", str(repo), "q", "search TokenBucket !file:tests lang:python"])
    out = capsys.readouterr().out
    assert rc == 0 and "repo:src/limiter.py:L1" in out and "test_limiter" not in out


def test_q_commits_touched_and_history(state_home, repo, capsys):
    from ctx.cli import main

    rc = main(["--workspace", str(repo), "q", "commits limiter | touched"])
    out = capsys.readouterr().out
    assert rc == 0 and "src/limiter.py · 2 commits" in out
    assert "tests/test_limiter.py · 1 commits" in out

    rc = main(["--workspace", str(repo), "q", "commits clamp type:diff"])
    out = capsys.readouterr().out
    assert rc == 0 and "0 rows" in out  # 'clamp' is in the message, not the diff

    rc = main(["--workspace", str(repo), "q", "search TokenBucket file:src | history"])
    out = capsys.readouterr().out
    assert rc == 0 and "2026-01-02 Ada" in out and "repo:src/limiter.py:L1" in out

    rc = main(["--workspace", str(repo), "q", "search max file:src | history --line"])
    out = capsys.readouterr().out
    assert rc == 0 and "repo:src/limiter.py:L3" in out and "2026-01-02 Ada" in out


def test_last_change_is_none_for_an_uncommitted_file(state_home, repo):
    from ctx.searchq import last_change

    ws = make_ws(repo)
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert last_change(ws, "new.py") is None
    row = last_change(ws, "src/limiter.py")
    assert row is not None and row.subject == "limiter: clamp negative takes"
