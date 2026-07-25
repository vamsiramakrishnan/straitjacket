"""One parser for ``git status --porcelain`` (R4).

Six call sites shell out to ``git status --porcelain``; three of them used
to carry their own parser, and the three disagreed. Each test below names
the disagreement it pins:

* quoted paths were never C-unescaped, so a non-ASCII filename became a
  path that does not exist (``caf\\303\\251.py``);
* ``" -> "`` was split out of *every* record instead of only rename/copy
  records, so the untracked file ``a -> b.txt`` was reported as ``b.txt"``;
* the scorecard parser did not unquote at all, so any path git quoted
  (which includes plain spaces) was counted but never read.

``ctx.gitstatus`` is now the single definition; these tests bind the
behaviour that ships.
"""

import os
import subprocess
from pathlib import Path

from ctx.gitstatus import PorcelainEntry, changed_paths, parse, unquote_bytes


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=ws, check=True, env=env,
                          capture_output=True)


def _porcelain(ws: Path) -> bytes:
    return _git(ws, "status", "--porcelain").stdout


# --------------------------------------------------------------- unit level
def test_unquote_handles_octal_escapes_and_c_escapes():
    assert unquote_bytes(rb'"caf\303\251.py"') == "café.py".encode("utf-8")
    assert unquote_bytes(rb'"tab\there.txt"') == b"tab\there.txt"
    assert unquote_bytes(rb'"a\"b.txt"') == b'a"b.txt'
    assert unquote_bytes(rb'"back\\slash"') == b"back\\slash"
    assert unquote_bytes(b"plain.txt") == b"plain.txt"


def test_rename_only_splits_on_rename_records():
    # A rename: destination side is the changed one; origin is preserved.
    (e,) = parse(b'R  base.txt -> "renamed base.txt"\n')
    assert e == PorcelainEntry("R", " ", "renamed base.txt", "base.txt")
    # An untracked file whose NAME contains " -> " must survive intact.
    (u,) = parse(b'?? "a -> b.txt"\n')
    assert u == PorcelainEntry("?", "?", "a -> b.txt", None)
    assert u.untracked and not u.renamed


def test_two_char_status_field_is_positional():
    entries = parse(b" M mod.py\nA  added.py\nUU conflict.py\n?? new.py\n")
    assert [(e.x, e.y, e.path) for e in entries] == [
        (" ", "M", "mod.py"),
        ("A", " ", "added.py"),
        ("U", "U", "conflict.py"),
        ("?", "?", "new.py"),
    ]


def test_ignored_and_ledger_entries_are_dropped_by_changed_paths():
    raw = b"!! build/\n?? .ctx-session-reads/x.json\n M keep.py\n"
    assert changed_paths(raw, exclude_top=".ctx-session-reads") == ["keep.py"]


# ------------------------------------------------------- against real git
def test_parser_matches_real_git_for_awkward_paths(git_workspace):
    ws = git_workspace
    for name in ("café.py", "a -> b.txt", "with space.txt", "plain.txt"):
        (ws / name).write_text("x\n", encoding="utf-8")
    _git(ws, "mv", "hello.py", "renamed hello.py")
    got = set(changed_paths(_porcelain(ws)))
    assert got == {
        "café.py",
        "a -> b.txt",
        "with space.txt",
        "plain.txt",
        "renamed hello.py",
    }


# ------------------------------------------------- consumers, end to end
def test_changed_files_snapshot_sees_quoted_and_arrow_paths(git_workspace, state_home):
    """facts.changed_files_snapshot fed `--changed`; the old parser returned
    ``caf\\303\\251.py`` (nonexistent) and ``b.txt"`` (wrong file)."""
    from ctx.facts import changed_files_snapshot
    from ctx.workspace import resolve_workspace

    ws_root = git_workspace
    (ws_root / "café.py").write_text("x\n", encoding="utf-8")
    (ws_root / "a -> b.txt").write_text("x\n", encoding="utf-8")
    _git(ws_root, "mv", "hello.py", "renamed hello.py")
    got = changed_files_snapshot(resolve_workspace(str(ws_root)))
    assert "café.py" in got
    assert "a -> b.txt" in got
    assert "renamed hello.py" in got  # renames must never be dropped
    assert not [p for p in got if "\\303" in p or p.endswith('"')]


def test_generation_hash_tracks_non_ascii_untracked_edits(git_workspace):
    """execution.generation_hash folds untracked (size, mtime_ns) in. With
    the un-unescaped path it could never stat the file, so an edit to
    ``café.py`` left the generation unchanged — a false 'nothing happened'."""
    from ctx.execution import generation_hash

    (git_workspace / "café.py").write_text("one\n", encoding="utf-8")
    g1 = generation_hash(git_workspace)
    (git_workspace / "café.py").write_text("two: longer\n", encoding="utf-8")
    g2 = generation_hash(git_workspace)
    assert g1 and g2 and g1 != g2


def test_workspace_fingerprint_tracks_non_ascii_untracked_edits(git_workspace):
    """plan_exec's node cache keyed on a fingerprint that skipped any path
    it could not stat — so editing café.py served a stale cached node."""
    from ctx.plan_exec import _workspace_fingerprint
    from ctx.workspace import resolve_workspace

    (git_workspace / "café.py").write_text("one\n", encoding="utf-8")
    f1 = _workspace_fingerprint(resolve_workspace(str(git_workspace)))
    (git_workspace / "café.py").write_text("two: longer\n", encoding="utf-8")
    f2 = _workspace_fingerprint(resolve_workspace(str(git_workspace)))
    assert f1 and f2 and f1 != f2


def test_scorecard_counts_lines_in_quoted_untracked_paths(git_workspace):
    """The scorecard parser never unquoted, so `"with space.txt"` (git quotes
    plain spaces) was counted as a new file but contributed zero lines."""
    from ctx.scorecard import attach_deliverable

    (git_workspace / "with space.txt").write_text("a\nb\nc\n", encoding="utf-8")
    sc = attach_deliverable({}, git_workspace)
    assert sc["deliverable"]["files_new"] == 1
    assert sc["deliverable"]["lines_new"] == 3
