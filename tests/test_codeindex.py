"""The text index (ctx.codeindex): a Zoekt-shaped trigram index that is a
candidate generator only, synced by fingerprint before every query and
verified against the live bytes by its callers.

Staleness is the whole point, so most of these tests edit the tree between
two queries and check that the second answer describes the tree on disk.
"""

from __future__ import annotations

import os
import re

import pytest

from conftest import make_store, make_ws


@pytest.fixture()
def repo(workspace_dir):
    (workspace_dir / "src").mkdir()
    (workspace_dir / "src" / "bucket.py").write_text(
        "class TokenBucket:\n    def take(self, n):\n        return n\n", encoding="utf-8"
    )
    (workspace_dir / "src" / "other.py").write_text(
        "def unrelated():\n    return 'nothing to see'\n", encoding="utf-8"
    )
    (workspace_dir / "README.md").write_text("# demo\nTokenBucket is the limiter.\n", encoding="utf-8")
    (workspace_dir / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    return workspace_dir


def _index(repo, state_home):
    from ctx.codeindex import Index

    ws = make_ws(repo)
    store = make_store(ws)
    return Index(store, ws)


# ------------------------------------------------------------ trigrams
def test_trigram_keys_are_lowercased_and_overlapping():
    from ctx.codeindex import trigram_keys

    keys = trigram_keys(b"AbCd")
    assert keys == {int.from_bytes(b"abc", "big"), int.from_bytes(b"bcd", "big")}
    assert trigram_keys(b"ab") == set()


def test_regex_expression_keeps_only_the_literal_runs_it_must_contain():
    from ctx.codeindex import expr_for_literal, expr_for_regex, is_unrestricted

    assert expr_for_regex("TokenBucket") == expr_for_literal("TokenBucket")
    # a breaker splits runs; both sides are required
    e = expr_for_regex(r"def resolve_refs\(")
    assert e[0] == "and" or e[0] == "tri"
    # alternation is an OR of its branches
    e = expr_for_regex("foo|bar")
    assert e[0] == "or" and len(e[1]) == 2
    # a branch with no literal makes the whole alternation unrestricted
    assert is_unrestricted(expr_for_regex("foo|.+"))
    # a repeat with min 0 requires nothing; min 1 requires its body
    assert is_unrestricted(expr_for_regex("(abc)*"))
    assert not is_unrestricted(expr_for_regex("(abc)+"))
    # too short to have a trigram
    assert is_unrestricted(expr_for_regex("x.y"))
    assert is_unrestricted(expr_for_regex("ab"))


def test_postings_round_trip():
    from ctx.codeindex import _decode_postings, _encode_postings

    ids = [0, 1, 5, 200, 100000]
    blob = _encode_postings(ids)
    assert _decode_postings(blob, 0, len(blob)) == ids


# --------------------------------------------------------------- sync
def test_first_sync_indexes_text_files_and_skips_binary(state_home, repo):
    idx = _index(repo, state_home)
    rc = idx.sync()
    assert rc["indexed"] == 4 and rc["binary"] == 1  # ctx.toml is text too
    assert set(idx.files) == {"src/bucket.py", "src/other.py", "README.md", "ctx.toml"}
    assert idx.built and idx.status()["segments"] == 1
    idx.close()


def test_candidates_are_a_superset_verified_by_the_caller(state_home, repo):
    from ctx.codeindex import expr_for_regex

    idx = _index(repo, state_home)
    idx.sync()
    got = idx.candidates(expr_for_regex("TokenBucket"))
    assert got == ["README.md", "src/bucket.py"]
    # case-insensitive by construction: the caller's regex decides the case
    assert idx.candidates(expr_for_regex("tokenbucket")) == ["README.md", "src/bucket.py"]
    assert idx.candidates(expr_for_regex("nothing to see")) == ["src/other.py"]
    assert idx.candidates(expr_for_regex("absent_term_zzz")) == []
    idx.close()


def test_an_edit_between_queries_is_seen_without_a_rebuild(state_home, repo):
    from ctx.codeindex import expr_for_regex

    idx = _index(repo, state_home)
    idx.sync()
    assert idx.candidates(expr_for_regex("brand_new_name")) == []
    p = repo / "src" / "other.py"
    p.write_text("def brand_new_name():\n    return 1\n", encoding="utf-8")
    # force a distinct mtime even on coarse filesystems
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    rc = idx.sync()
    assert rc["indexed"] == 1 and rc["segments"] == 2
    assert idx.candidates(expr_for_regex("brand_new_name")) == ["src/other.py"]
    # the old posting for other.py is dead: 'nothing to see' is gone
    assert idx.candidates(expr_for_regex("nothing to see")) == []
    idx.close()


def test_a_touch_without_a_content_change_does_not_reindex(state_home, repo):
    idx = _index(repo, state_home)
    idx.sync()
    p = repo / "src" / "other.py"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    rc = idx.sync()
    assert rc["indexed"] == 0 and rc["segments"] == 1
    assert idx.files["src/other.py"][3] == st.st_mtime_ns + 10**9
    idx.close()


def test_a_deleted_file_leaves_the_catalog(state_home, repo):
    from ctx.codeindex import expr_for_regex

    idx = _index(repo, state_home)
    idx.sync()
    (repo / "README.md").unlink()
    rc = idx.sync()
    assert rc["removed"] == 1
    assert idx.candidates(expr_for_regex("TokenBucket")) == ["src/bucket.py"]
    idx.close()


def test_symbols_ride_the_index(state_home, repo):
    idx = _index(repo, state_home)
    idx.sync()
    assert idx.files_defining("TokenBucket") == [("src/bucket.py", "class", 1)]
    assert idx.files_defining("take")[0][0] == "src/bucket.py"
    assert idx.files_defining("nope") == []
    assert ("src/bucket.py", "class", 1) in idx.files_defining("token", exact=False)
    idx.close()


def test_compaction_folds_segments_and_keeps_answers(state_home, repo, monkeypatch):
    from ctx import codeindex
    from ctx.codeindex import expr_for_regex

    monkeypatch.setattr(codeindex, "MAX_SEGMENTS", 1)
    idx = _index(repo, state_home)
    idx.sync()
    p = repo / "src" / "other.py"
    for i in range(3):
        p.write_text(f"def version_{i}():\n    return {i}\n", encoding="utf-8")
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + (i + 1) * 10**9))
        rc = idx.sync()
    assert rc["compacted"] and idx.status()["segments"] == 1
    assert idx.candidates(expr_for_regex("version_2")) == ["src/other.py"]
    assert idx.candidates(expr_for_regex("version_0")) == []
    assert idx.files_defining("version_2") == [("src/other.py", "function", 1)]
    idx.close()


def test_open_index_refuses_an_implicit_build_of_a_large_corpus(state_home, repo, monkeypatch):
    from ctx import codeindex

    monkeypatch.setattr(codeindex, "IMPLICIT_BUILD_MAX_FILES", 1)
    ws = make_ws(repo)
    store = make_store(ws)
    assert codeindex.open_index(store, ws) is None
    idx = codeindex.open_index(store, ws, build=True)
    assert idx is not None and idx.built
    idx.close()
    # built once, the guard no longer applies
    idx = codeindex.open_index(store, ws)
    assert idx is not None
    idx.close()


def test_the_index_can_be_switched_off(state_home, repo, monkeypatch):
    from ctx import codeindex

    monkeypatch.setenv("CTX_SEARCH_INDEX", "off")
    ws = make_ws(repo)
    assert codeindex.open_index(make_store(ws), ws) is None


# ---------------------------------------------------------- search verb
def test_search_uses_the_index_and_verifies_against_the_file(state_home, repo, monkeypatch):
    from ctx.retrieval import search

    monkeypatch.delenv("CTX_SEARCH_ENGINE", raising=False)
    ws = make_ws(repo)
    store = make_store(ws)
    out = search(store, ws, "repo:", ["TokenBucket"])
    assert "src/bucket.py:" in out and "README.md:" in out
    assert "index trigram" in out and "scanned: 2 of 5 targets" in out
    # case-sensitive by default: the lowercased pattern finds nothing even
    # though the index's candidates are the same two files
    out = search(store, ws, "repo:", ["tokenbucket"])
    assert "matches: 0" in out
    # a pattern with no trigram takes the ordinary engines
    out = search(store, ws, "repo:", ["x.y"])
    assert "index trigram" not in out


def test_search_sees_an_edit_made_after_the_first_query(state_home, repo, monkeypatch):
    from ctx.retrieval import search

    monkeypatch.delenv("CTX_SEARCH_ENGINE", raising=False)
    ws = make_ws(repo)
    store = make_store(ws)
    assert "matches: 0" in search(store, ws, "repo:", ["freshly_added"])
    p = repo / "src" / "other.py"
    p.write_text("freshly_added = 1\n", encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    out = search(store, ws, "repo:", ["freshly_added"])
    assert "src/other.py:" in out and "L1: freshly_added = 1" in out


def test_index_command_builds_and_reports(state_home, repo, capsys):
    from ctx.cli import main

    assert main(["--workspace", str(repo), "index", "--text"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[ctx index · text · 4 files") and "python 2" in out
    assert main(["--workspace", str(repo), "index", "--status"]) == 0
    out = capsys.readouterr().out
    assert "text: 4 files indexed" in out and "scip: none" in out


def test_regex_candidates_never_lose_a_true_match(state_home, repo):
    """Property check over a handful of patterns: every file the Python
    regex matches is among the index's candidates."""
    from ctx.codeindex import expr_for_regex

    idx = _index(repo, state_home)
    idx.sync()
    texts = {rel: (repo / rel).read_text(encoding="utf-8") for rel in idx.files}
    for pat in ["Token", "take|unrelated", r"def \w+\(", "(see)+", "limiter\\.", "return"]:
        rx = re.compile(pat)
        truth = sorted(rel for rel, t in texts.items() if rx.search(t))
        cands = idx.candidates(expr_for_regex(pat))
        assert set(truth) <= set(cands), (pat, truth, cands)
    idx.close()
