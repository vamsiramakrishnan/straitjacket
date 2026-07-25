"""One name for the 160-char line clip (R5).

The literal ``160`` appeared 24 times, plus four private ``_LINE_CAP = 160``
constants. Every one of them answers the same question — how wide may one
line of quoted foreign text be inside a bounded row — so they are now one
named constant. These tests pin the name, the value, and (just as important)
the clip widths that are NOT the same decision and must stay separate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ctx.textutil import EVIDENCE_LINE_CHARS

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "ctx"


def test_the_value_is_unchanged():
    assert EVIDENCE_LINE_CHARS == 160


def test_the_literal_is_gone_from_the_renderers():
    """The N+1th-author guard: a new row renderer must reach for the name."""
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "textutil.py":  # the definition and its prose
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\[:\s*160\s*\]|(?<![\w.])160\b(?!\s*\))", line):
                if line.lstrip().startswith("#"):
                    continue
                offenders.append(f"{py.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "use ctx.textutil.EVIDENCE_LINE_CHARS instead of the bare literal: "
        f"{offenders}"
    )


def test_no_module_keeps_a_private_alias_at_this_width():
    """Four modules had their own ``_LINE_CAP = 160``."""
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "textutil.py":  # the definition itself
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"_?[A-Z_]+\s*=\s*160\s*(#.*)?$", line):
                offenders.append(f"{py.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, offenders


# ------------------------------------------- what deliberately stays apart
def test_the_other_clip_widths_are_still_their_own_decision():
    """A shared constant two callers want to move in opposite directions is
    worse than two literals. These four all clip a line, at 200, for reasons
    that are not this one — see the constant's docstring."""
    search = (SRC / "_retrieval" / "search.py").read_text(encoding="utf-8")
    assert "_LINE_CHARS = 200" in search, "search HIT width is not the census width"

    jobs = (SRC / "jobs.py").read_text(encoding="utf-8")
    assert "_CLIP_COLS = 200" in jobs
    # …and it is not even the same MECHANISM: it appends an ellipsis.
    assert "…" in jobs

    facts = (SRC / "facts.py").read_text(encoding="utf-8")
    assert "_LINE_CAP = 200" in facts, "a whole-ROW bound, not a per-line one"


def test_it_is_a_width_not_a_helper():
    """Call sites strip, str(), or slice differently around the clip; a
    shared ``clip()`` would have to grow a flag per caller. The one thing
    they genuinely share is the number, so that is what is shared."""
    from ctx import textutil

    assert isinstance(EVIDENCE_LINE_CHARS, int)
    assert not callable(EVIDENCE_LINE_CHARS)
    assert not hasattr(textutil, "clip_line")


# --------------------------------------------------- it is actually applied
def _refs_row(tmp_path, state_home, total: int) -> str:
    """Render ``ctx q refs`` over a source line of exactly ``total``
    characters and return the row's quoted payload."""
    from conftest import make_store, make_ws
    from ctx.query import run_query

    root = tmp_path / f"p{total}"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    stem = "use = target  # "
    line = stem + "z" * (total - len(stem))
    assert len(line) == total
    (root / "a.py").write_text("target = 1\n" + line + "\n", encoding="utf-8")
    ws = make_ws(root)
    store = make_store(ws)
    rendered, code = run_query(ws, store, "refs target")
    assert code == 0, rendered
    rows = [ln for ln in rendered.splitlines() if ln.startswith("repo:a.py:L2:")]
    assert rows, rendered
    return rows[0].split(": ", 1)[1]


def test_a_long_row_is_cut_at_the_named_width(tmp_path, state_home):
    """End-to-end through ``ctx q refs`` — one of the four modules that kept
    its own ``_LINE_CAP = 160``."""
    payload = _refs_row(tmp_path, state_home, EVIDENCE_LINE_CHARS + 40)
    assert len(payload) == EVIDENCE_LINE_CHARS


@pytest.mark.parametrize("total", [1, 80, 159, 160, 161, 400])
def test_the_boundary_is_exact(tmp_path, state_home, total):
    """A line shorter than the cap survives whole; a longer one is cut to
    exactly the cap. An off-by-one in the shared constant cannot hide."""
    payload = _refs_row(tmp_path, state_home, max(total, 20))
    assert len(payload) == min(max(total, 20), EVIDENCE_LINE_CHARS)
