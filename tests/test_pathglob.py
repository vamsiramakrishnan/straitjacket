"""One glob dialect, asserted at every door that has one.

A bug bash proved end to end through the CLI what the debt ledger had only
described (id 7884ed9a7d, "two glob dialects"): ``--glob 'src/*.py'``
reached ``src/sub/nested.py`` in ``search`` and ``corpus`` because those ran
raw ``fnmatch``, where ``*`` crosses ``/``, while ``is_ignored`` and
``rg --glob`` use gitwildmatch, where it does not. Same pattern, same path,
two answers -- a silent scope leak one way and a silent over-exclusion the
other.

These tests pin the dialect itself and then pin that each caller is on it,
because "one matcher" is only true while nobody adds a second.
"""

from __future__ import annotations

import pytest

from ctx import pathglob


# --------------------------------------------------- the boundary rule
@pytest.mark.parametrize(
    "rel,pattern,expected",
    [
        # The defect, stated directly.
        ("src/top.py", "src/*.py", True),
        ("src/sub/nested.py", "src/*.py", False),
        ("src/sub/deep/n.py", "src/*.py", False),
        # ** is how you ask to cross a separator.
        ("src/sub/nested.py", "src/**/*.py", True),
        ("src/sub/deep/n.py", "src/**", True),
        # A pattern with no separator matches by basename at any depth.
        ("src/a.py", "*.py", True),
        ("a.py", "*.py", True),
        # The convention the old fnmatch matcher special-cased is native here.
        ("x.py", "**/x.py", True),
        ("a/b/x.py", "**/x.py", True),
        # ? is bounded the same way * is.
        ("src/a.py", "src/?.py", True),
        ("src/a/b.py", "src/?/b.py", True),
        ("src/ab.py", "src/?.py", False),
        # Character classes survive translation.
        ("src/a1.py", "src/a[0-9].py", True),
        ("src/ax.py", "src/a[0-9].py", False),
    ],
)
def test_dialect(rel, pattern, expected):
    assert pathglob.matches(rel, pattern) is expected


def test_leading_bang_is_a_path_not_a_negation():
    """`!` leads a gitignore LINE. A selector is one pattern with nowhere to
    negate to, so inverting the caller's request would be the surprise."""
    assert pathglob.matches("a.py", "!a.py") is False
    assert pathglob.matches("!a.py", "!a.py") is True


def test_empty_pattern_matches_nothing():
    assert pathglob.matches("a.py", "") is False


def test_leading_dot_slash_is_stripped():
    assert pathglob.matches("./src/top.py", "src/*.py") is True


# --------------------------------------- the fallback speaks the same dialect
def test_stdlib_fallback_keeps_the_boundary_rule():
    """The fallback exists so a broken pathspec install degrades instead of
    dying -- but a fallback that reverts to fnmatch would just reintroduce
    the split under a rarer condition."""
    from ctx.pathglob import _translate

    import re

    def fb(rel, pat):
        return re.match(_translate(pat), rel) is not None

    assert fb("src/top.py", "src/*.py") is True
    assert fb("src/sub/nested.py", "src/*.py") is False
    assert fb("x.py", "**/x.py") is True
    assert fb("a/b/x.py", "**/x.py") is True
    assert fb("src/a.py", "*.py") is True
    assert fb("node_modules/a/b.js", "node_modules/**") is True


# ------------------------------------------------ every door, one matcher
def test_every_path_glob_caller_is_on_the_shared_matcher():
    """The dedup, as an invariant rather than a moment.

    The split lasted as long as it did because each site was locally
    reasonable; only comparing two of them showed the disagreement. A grep
    is a blunt instrument, but it is the one that would have caught this.
    """
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "ctx"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "pathglob.py":
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bfnmatch\.(fnmatch|fnmatchcase)\s*\(", line):
                offenders.append(f"{path.relative_to(src)}:{i}: {line.strip()}")
    assert not offenders, (
        "path matching outside ctx.pathglob -- fnmatch's '*' crosses '/' and "
        "will disagree with the ignore matcher:\n  " + "\n  ".join(offenders)
    )


def test_search_glob_does_not_reach_into_a_subdirectory(state_home, workspace_dir):
    """The CLI-level consequence: a scope leak in `ctx search`."""
    from ctx._retrieval.targets import _glob_match

    assert _glob_match("src/top.py", "src/*.py") is True
    assert _glob_match("src/sub/nested.py", "src/*.py") is False


def test_corpus_exclude_does_not_over_exclude(state_home, workspace_dir):
    """The other consequence: a file silently unfindable through `corpus`."""
    from ctx.filesets import _glob_match

    assert _glob_match("src/sub/nested.py", "src/*.py") is False
