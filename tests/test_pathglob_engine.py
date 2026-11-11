"""ctx.pathglob's bounded engine: identical to pathspec, never exponential.

pathspec compiles a gitignore pattern to a Python regex, and `re.match`
backtracks: `*a` x14 + `*b0` against `aaaa...a.png` took 10.5 s at 30
characters in review, and a 1,000-glob `.ctxignore` ran past 120 s. The new
engine runs the same semantics as a position-set sweep. These tests prove
"same" differentially against pathspec itself, and "bounded" by scaling.
"""

from __future__ import annotations

import random
import time

import pytest

from ctx import pathglob

pathspec = pytest.importorskip("pathspec")
# pathspec compiles "[[..." classes with a nested-set FutureWarning; noise here.
pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

_PATTERN_ATOMS = ["a", "b", "A", ".", "x", "*", "*", "**", "?", "[ab]", "[!a]", "[^b]",
                  "[a-c]", "[]a]", "[", "\\*", "\\?", "/", "/", "-", " ", "\\ "]
_PATH_ATOMS = ["a", "b", "A", "x", ".", "-", "ab", "aa", "]", "[", "*", " ", "\n"]


def _old(pattern: str, rel: str) -> bool | None:
    """The previous engine: pathspec's regex, or None when pathspec rejects it."""
    pattern = pattern.removeprefix("./")  # as the old pathglob.matches did
    if not pattern:
        return False
    literal = "\\" + pattern if pattern[:1] in ("!", "#") else pattern
    try:
        spec = pathspec.PathSpec.from_lines("gitignore", [literal])
    except Exception:
        return None
    return bool(spec.match_file(rel.removeprefix("./")))


def _gen_pattern(rng: random.Random) -> str:
    p = "".join(rng.choice(_PATTERN_ATOMS) for _ in range(rng.randint(1, 7)))
    if rng.random() < 0.15:
        p = "/" + p
    if rng.random() < 0.15:
        p = p + "/"
    if rng.random() < 0.08:
        p = rng.choice("!#") + p
    return p


def _gen_path(rng: random.Random) -> str:
    segs = ["".join(rng.choice(_PATH_ATOMS) for _ in range(rng.randint(1, 3)))
            for _ in range(rng.randint(1, 5))]
    rel = "/".join(segs)
    r = rng.random()
    if r < 0.1:
        rel = "/" + rel
    elif r < 0.2:
        rel = "./" + rel
    elif r < 0.25:
        rel = rel + "/"
    return rel


def test_differential_against_pathspec():
    rng = random.Random(20261111)
    compared = 0
    fixed = [("*.py", "src/a.py"), ("src/*.py", "src/sub/a.py"), ("a/**/b", "a/x/y/b"),
             ("**/x", "x"), ("a/", "a/b"), ("a/", "a"), ("/a", "a/b"), ("a**b", "axxb"),
             ("x[!a]y", "x/y"), ("[a-c]", "b"), ("A", "a"), ("**", "q"), ("*/", "a/b"),
             ("**/*/**", "a/b"), ("!x", "!x"), ("#x", "#x"), ("a\\ ", "a "), ("a ", "a")]
    pairs = list(fixed)
    for _ in range(6000):
        pairs.append((_gen_pattern(rng), _gen_path(rng)))
    mismatches = []
    for pattern, rel in pairs:
        want = _old(pattern, rel)
        if want is None:
            continue
        got = pathglob.matches(rel, pattern)
        compared += 1
        if got != want:
            mismatches.append((pattern, rel, want, got))
    assert compared > 4000
    assert not mismatches, mismatches[:20]


@pytest.mark.parametrize("n", [20, 40, 80, 190])
def test_the_review_pattern_is_bounded(n):
    pattern = "*a" * 14 + "*b0"
    name = "a" * (n - 4) + ".png"
    t = time.perf_counter()
    assert pathglob.matches(name, pattern) is False
    assert time.perf_counter() - t < 0.05


def test_a_thousand_glob_ignore_file_is_bounded():
    globs = [("*a" * 14) + f"*b{i}" for i in range(1000)]
    name = "a" * 186 + ".png"
    t = time.perf_counter()
    assert not any(pathglob.matches(name, g) for g in globs)
    elapsed = time.perf_counter() - t
    assert elapsed < 0.5, elapsed  # was > 120 s; measured ~50 ms cold
