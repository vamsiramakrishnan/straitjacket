"""One helper for the house short id (R12).

``str(x).removeprefix("sha256:")[:12]`` was retyped at 18 call sites, plus
another 10 in the already-stripped spelling ``x[:12]``. Nothing disagreed;
the cost was that the display width lived in 28 places. These tests pin the
one definition, the edge cases the inline copies handled implicitly, and —
just as important — the look-alikes that must NOT be folded in.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ctx.textutil import SHORT_ID_CHARS, short_id, short_path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "ctx"

FULL = "a1b2c3d4e5f6" + "0" * 52  # 64 hex chars


def test_strips_the_prefix_and_clips():
    assert short_id("sha256:" + FULL) == "a1b2c3d4e5f6"
    assert len(short_id("sha256:" + FULL)) == SHORT_ID_CHARS


def test_already_stripped_input_is_the_same_answer():
    """Half the call sites had the prefix, half had a bare store hash. One
    helper has to serve both or it would not have replaced both."""
    assert short_id(FULL) == short_id("sha256:" + FULL)


def test_already_short_input_passes_through():
    assert short_id("abc") == "abc"
    assert short_id("sha256:abc") == "abc"


@pytest.mark.parametrize("empty", ["", None, 0, False])
def test_empty_in_empty_out(empty):
    assert short_id(empty) == ""


def test_non_string_input_is_stringified():
    assert short_id(1234567890123456) == "123456789012"


def test_only_the_leading_prefix_is_removed():
    """`removeprefix`, not `replace` — a hash that somehow contains the
    literal later must not be mangled."""
    assert short_id("sha256:sha256:" + FULL) == "sha256:a1b2c"


def test_no_whitespace_stripping():
    """The inline copies did not strip, and this helper replaced them
    verbatim. ``ctx.facts._short_id`` adds its own strip on top, for values
    that arrive from CLI arguments rather than from manifests."""
    assert short_id("  abc") == "  abc"


# ------------------------------------------------------- the facts dialect
def test_facts_short_id_keeps_its_own_contract():
    from ctx.facts import SHORT_ID, _short_id

    assert SHORT_ID == SHORT_ID_CHARS
    assert _short_id("sha256:" + FULL) == "a1b2c3d4e5f6"
    # None, not "" — the fact store distinguishes "no id" from "empty id"
    assert _short_id("") is None
    assert _short_id(None) is None
    # strip AFTER the prefix, exactly as before: order matters here
    assert _short_id("sha256:  abc  ") == "abc"


# --------------------------------------------------- what stays duplicated
def test_it_is_not_the_path_shortener():
    """``_short`` used to mean both things. They are not substitutable."""
    assert short_path("src/ctx/digest/lintprof.py") == "digest/lintprof.py"
    assert short_id("src/ctx/digest/lintprof.py") == "src/ctx/dige"
    assert short_id is not short_path


def test_id_minting_widths_are_deliberately_not_shared():
    """``sha256(...).hexdigest()[:12]`` in ctx.reflex (intervention id),
    ctx.resolver (plan id) and ctx.policy (epoch id) is the same NUMBER for a
    different REASON: those widths are a stored identity's collision budget,
    the display width is a readability choice. Folding them together would
    make widening one silently widen the other. This test exists so the
    decision is recorded rather than rediscovered."""
    minters = {
        "reflex.py": "intervention id",
        "resolver.py": "plan id",
        "policy.py": "epoch id",
    }
    for name in minters:
        text = (SRC / name).read_text(encoding="utf-8")
        assert re.search(r"hexdigest\(\)\[:12\]", text), (
            f"{name} no longer mints its id at 12 hex — if it now calls "
            "short_id, delete this test and record why the two widths are "
            "in fact one decision."
        )


def test_git_object_names_are_not_house_short_ids():
    """``ws.git.head[:12]`` is a different namespace with its own
    conventions; git's own abbreviation length is adaptive."""
    for name in ("checkpoint.py", "_retrieval/stats.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert "head[:12]" in text or "head else" in text


# ------------------------------------------------------- no copies survive
def test_the_prefix_and_clip_idiom_is_gone_from_python():
    """The N+1th-author guard for the unambiguous spelling."""
    offenders = []
    for py in sorted((REPO / "src").rglob("*.py")):
        if py.name == "textutil.py":  # the docstring naming the idiom it replaced
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""removeprefix\(['"]sha256:['"]\)\[:\d+\]""", line):
                offenders.append(f"{py.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, (
        f"use ctx.textutil.short_id instead of retyping the idiom: {offenders}"
    )


def test_the_helper_is_actually_used_where_handles_are_rendered():
    """A guard against the opposite failure: the helper exists but the call
    sites quietly drift back to literals."""
    users = set()
    for py in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "short_id":
                users.add(py.relative_to(SRC).as_posix())
    # every module family that renders a content-addressed handle
    for expected in (
        "_retrieval/get.py",
        "_retrieval/search.py",
        "_retrieval/stats.py",
        "digest/__init__.py",
        "checkpoint.py",
        "codeverbs.py",
        "query.py",
        "rundiff.py",
        "seq.py",
    ):
        assert expected in users, expected
