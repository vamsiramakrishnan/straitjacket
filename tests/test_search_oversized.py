"""``ctx search repo:<file>`` on a file over the index's 1 MiB limit.

Measured: the Read guard tells the agent to ``ctx search repo:server.log
PATTERN``; the text index skips files over 1 MiB, so on a 2 MB log the
search printed "matches: 0" although the pattern was there.
"""

from __future__ import annotations

import pytest

from conftest import make_store, make_ws


@pytest.fixture()
def big_repo(workspace_dir, monkeypatch):
    monkeypatch.delenv("CTX_SEARCH_ENGINE", raising=False)
    monkeypatch.delenv("CTX_SEARCH_INDEX", raising=False)
    (workspace_dir / "src").mkdir()
    (workspace_dir / "src" / "a.py").write_text("def f():\n    return 'connection ok'\n",
                                                encoding="utf-8")
    with open(workspace_dir / "server.log", "w", encoding="utf-8") as fh:
        for i in range(30000):
            if i == 21345:
                fh.write("ERROR db connection reset by peer at pool alpha\n")
            else:
                fh.write(f"2025-02-11 09:00:{i % 60:02d} INFO request {i} served in "
                         f"{i % 97}ms from upstream pool alpha\n")
    assert (workspace_dir / "server.log").stat().st_size > 2 * 1024 * 1024
    return workspace_dir


def _search(repo, ref, patterns, **kw):
    from ctx.retrieval import search

    ws = make_ws(repo)
    return search(make_store(ws), ws, ref, patterns, **kw)


def test_a_named_file_over_the_index_limit_is_searched(state_home, big_repo):
    out = _search(big_repo, "repo:server.log", ["connection reset"], context=1)
    assert "matches: 1 · shown: 1" in out
    assert " >L21346: ERROR db connection reset by peer at pool alpha" in out
    assert "  L21345: 2025-02-11 09:00:44 INFO request 21344" in out
    assert "  L21347: 2025-02-11 09:00:46 INFO request 21346" in out
    assert "over the 1 MiB index limit scanned directly" in out


def test_direct_scan_counts_every_match_and_caps_what_it_shows(state_home, big_repo):
    out = _search(big_repo, "repo:server.log", ["served in 5ms"], max_matches=3)
    # i % 97 == 5 for 309 of the 30,000 lines (line 21346 is the ERROR line)
    expected = sum(1 for i in range(30000) if i % 97 == 5 and i != 21345)
    assert f"matches: {expected} · shown: 3 · truncated" in out
    assert "L6: 2025-02-11 09:00:05 INFO request 5 served in 5ms" in out


def test_direct_scan_line_numbers_cross_block_boundaries(state_home, big_repo):
    out = _search(big_repo, "repo:server.log", ["request 2999[0-9] served"])
    assert "matches: 10 · shown: 10" in out
    assert "L30000: 2025-02-11 09:00:59 INFO request 29999 served" in out


def test_direct_scan_redacts(state_home, big_repo):
    with open(big_repo / "server.log", "a", encoding="utf-8") as fh:
        fh.write("token leaked ghp_" + "A" * 36 + " in request\n")
    out = _search(big_repo, "repo:server.log", ["token leaked"])
    assert "matches: 1" in out and "ghp_AAAA" not in out
    assert "redact" in out


def test_mode_all_requires_every_pattern_in_the_oversized_file(state_home, big_repo):
    out = _search(big_repo, "repo:server.log", ["connection reset", "no_such_thing_here"],
                  mode_all=True)
    assert "matches: 0" in out
    out = _search(big_repo, "repo:server.log", ["connection reset", "pool alpha"], mode_all=True,
                  max_matches=2)
    assert "matches: 30,000" in out


def test_a_wide_search_says_which_files_it_did_not_search(state_home, big_repo, monkeypatch):
    for i in range(10):
        (big_repo / "src" / f"m{i}.py").write_text(f"x{i} = 'unrelated'\n", encoding="utf-8")
    out = _search(big_repo, "repo:", ["connection reset"])
    assert "matches: 0" in out
    assert ("1 files over the 1 MiB index limit were not searched: server.log "
            "(name one to search it: ctx search repo:<file> PATTERN)") in out


@pytest.mark.parametrize("block", [4096, 65536])
def test_context_lines_come_from_neighbouring_blocks(state_home, big_repo, monkeypatch, block):
    from ctx._retrieval import search as search_mod

    monkeypatch.setattr(search_mod, "_DIRECT_BLOCK", block)
    out = _search(big_repo, "repo:server.log", ["request (21344|21346) served", "connection reset"],
                  context=2)
    assert "matches: 3 · shown: 3" in out
    for n in (21343, 21344, 21345, 21346, 21347, 21348, 21349):
        assert f"L{n}: " in out
    assert " >L21346: ERROR db connection reset" in out
