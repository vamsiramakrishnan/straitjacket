"""Content-anchored addresses: a ``repo:`` line span that survives an edit.

The invariant under test is the one straitjacket advertises everywhere else --
*the same address returns the same bytes* -- extended to the one address family
that could not keep it. A line number into a live worktree file is a position,
not an identity, so before anchors ``ctx get repo:m.py --lines 4:5`` would
happily return different lines after an edit, exit 0, and say nothing.

Three outcomes are pinned here because the mechanism is only worth having if a
reader can tell them apart: verified (silent), relocated (declared, and the
right bytes come back), lost (refused, non-zero, with a way forward).
"""

import pytest

from conftest import make_store, make_ws

from ctx import anchors
from ctx.retrieval import RetrievalError, Selector, _span, _span_anchored, get

BEFORE = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n\ndef gamma():\n    return 3\n"


def _seed(root, text=BEFORE):
    (root / "m.py").write_text(text, encoding="utf-8")


def _get(store, ws, spec, **kw):
    a, b, anchor = _span_anchored(spec)
    return get(store, ws, "repo:m.py", Selector(lines=(a, b), lines_anchor=anchor, **kw))


# ----------------------------------------------------------------- the pure core


def test_anchor_is_position_free():
    """The property relocation is built on: equal content, equal anchor.

    If the anchor mixed in a line number, content that moved would anchor
    differently after moving and could never be found again -- the mechanism
    would degrade to a tripwire that only ever says "gone".
    """
    assert anchors.anchor(["b", "c"]) == anchors.anchor(["b", "c"])
    assert anchors.anchor(["b", "c"]) != anchors.anchor(["c", "b"])
    assert anchors.anchor([]) != anchors.anchor([""])


def test_line_tag_and_anchor_agree_about_a_line():
    """Both digests derive from one per-line hash, so a tag can never describe
    a line differently from that line's contribution to a span's anchor."""
    assert anchors.line_tag("x") != anchors.line_tag("y")
    assert len(anchors.line_tag("x")) == anchors.LINE_TAG_CHARS
    assert len(anchors.anchor(["x"])) == anchors.ANCHOR_CHARS


def test_relocate_prefers_the_nearest_identical_window():
    """Duplicate content is common (boilerplate, repeated guards). Every match
    is an equally correct answer, so the nearest one is chosen: it produces the
    smallest 'moved from → to' note for a reader to check."""
    lines = ["dup", "dup", "x", "dup", "dup", "y", "dup", "dup"]
    want = anchors.anchor(["dup", "dup"])
    assert anchors.relocate(lines, want, 2, near=5) == 4
    assert anchors.relocate(lines, want, 2, near=1) == 1


def test_relocate_reports_absence_rather_than_guessing():
    assert anchors.relocate(["a", "b"], anchors.anchor(["q", "z"]), 2, near=1) is None
    # A span longer than the file cannot be anywhere in it; the scan must not
    # fall through to a truncated comparison that "matches" a shorter window.
    assert anchors.relocate(["a"], anchors.anchor(["a", "b"]), 2, near=1) is None


def test_span_grammar_round_trips_and_rejects_junk():
    assert anchors.parse_span("4:5") == (4, 5, None)
    assert anchors.parse_span("4:5@0a1b2c3d") == (4, 5, "0a1b2c3d")
    assert anchors.format_span(4, 5, "0a1b2c3d") == "4:5@0a1b2c3d"
    assert anchors.format_span(4, 5, None) == "4:5"
    for bad in ("4:5@short", "4:5@ZZZZZZZZ", "5:4", "0:3", "4"):
        with pytest.raises(ValueError):
            anchors.parse_span(bad)


def test_only_line_selectors_take_an_anchor():
    """Silently dropping the anchor would accept an address that LOOKS verified
    and is not -- the exact failure this grammar exists to close. --bytes and
    --records index an immutable stream, so an anchor there promises nothing."""
    with pytest.raises(RetrievalError):
        _span("4:5@0a1b2c3d")
    assert _span("4:5") == (4, 5)


# ------------------------------------------------------------- end to end


def test_repo_reads_mint_an_anchor_and_immutable_reads_do_not(state_home, workspace_dir):
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)

    out = _get(store, ws, "4:5")
    assert "L4: def beta():" in out
    anchor = anchors.anchor(["def beta():", "    return 2"])
    assert f"selector: --lines 4:5@{anchor}" in out

    # An immutable ref cannot go stale, so it pays nothing for a staleness
    # check: no anchor is minted on run:/blob:/snapshot: line spans.
    from ctx.execution import snapshot_file

    snap = snapshot_file(store, ws, "m.py")
    frozen = get(store, ws, f"snapshot:{snap['id']}", Selector(lines=(4, 5)))
    assert "selector: --lines 4:5 of" in frozen
    assert "@" not in frozen.split("selector:")[1].split("\n")[0]


def test_unanchored_address_still_drifts_silently(state_home, workspace_dir):
    """The behaviour anchors are an opt-in fix for, pinned so the cost of NOT
    using one stays visible: same address, different content, exit 0."""
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    assert "L4: def beta():" in _get(store, ws, "4:5")
    (workspace_dir / "m.py").write_text("import os\nimport sys\n" + BEFORE, encoding="utf-8")
    assert "L4: def beta():" not in _get(store, ws, "4:5")


def test_anchored_address_follows_content_that_moved(state_home, workspace_dir):
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    anchor = anchors.anchor(["def beta():", "    return 2"])

    (workspace_dir / "m.py").write_text("import os\nimport sys\n" + BEFORE, encoding="utf-8")
    out = _get(store, ws, f"4:5@{anchor}")

    assert f"anchor: @{anchor} moved L4:5 → L6:7 (content unchanged)" in out
    assert "L6: def beta():" in out
    assert "L7:     return 2" in out
    # The echoed selector is the CORRECTED address, so the next turn's copy of
    # it points where the content actually is.
    assert f"selector: --lines 6:7@{anchor}" in out


def test_anchored_address_survives_the_file_shrinking_past_it(state_home, workspace_dir):
    """Deleting lines above the span leaves the address pointing past EOF. The
    range check refuses a start past the end, so anchor resolution has to run
    BEFORE clamping -- otherwise this reports 'selects nothing' about content
    sitting three lines further up."""
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    anchor = anchors.anchor(["def gamma():", "    return 3"])
    assert f"selector: --lines 7:8@{anchor}" in _get(store, ws, "7:8")

    (workspace_dir / "m.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    out = _get(store, ws, f"7:8@{anchor}")
    assert "moved L7:8 → L1:2" in out
    assert "L1: def gamma():" in out


def test_lost_anchor_refuses_instead_of_answering_a_different_question(
    state_home, workspace_dir
):
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    anchor = anchors.anchor(["def beta():", "    return 2"])

    (workspace_dir / "m.py").write_text(
        "def alpha():\n    return 1\n\ndef gamma():\n    return 3\n", encoding="utf-8"
    )
    with pytest.raises(RetrievalError) as e:
        _get(store, ws, f"4:5@{anchor}")
    # A refusal that does not say what to do next is a dead end, and the house
    # rule is that omission stays reversible.
    assert "no longer in this file" in str(e.value)
    assert "ctx get repo:m.py --lines 4:5" in str(e.value)


def test_verified_read_is_byte_identical_to_the_unanchored_one(state_home, workspace_dir):
    """A verified anchor changes nothing about the answer -- no extra note, no
    reflow. Declaring omissions is house style; narrating successes is window
    tax, and it would also break digest determinism for callers that anchor."""
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    plain = _get(store, ws, "4:5")
    anchor = anchors.anchor(["def beta():", "    return 2"])
    assert _get(store, ws, f"4:5@{anchor}") == plain


def test_hashlines_tag_every_line_it_renders(state_home, workspace_dir):
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    out = _get(store, ws, "4:5", hashlines=True)
    assert f"L4:{anchors.line_tag('def beta():')}| def beta():" in out
    assert f"L5:{anchors.line_tag('    return 2')}|     return 2" in out
    # Off by default: every existing digest, receipt and test depends on the
    # untagged shape being byte-identical.
    assert "L4: def beta():" in _get(store, ws, "4:5")


def test_continuations_carry_the_anchor_forward(state_home, workspace_dir):
    """An address that sheds its anchor at the first budget cut would leave the
    reader following a chain that silently stops being verifiable."""
    body = "".join(f"line {i}\n" for i in range(1, 400))
    _seed(workspace_dir, body)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    out = _get(store, ws, "1:399")
    nxt = [ln for ln in out.splitlines() if ln.startswith("next:")]
    assert nxt, out
    assert "@" in nxt[0], nxt[0]


def test_def_hands_the_editor_a_verifiable_address(state_home, workspace_dir):
    """`ctx def` is the verb that runs immediately before an edit, so its
    address is the one most likely to be used after the file has moved."""
    from ctx.codeverbs import cmd_def

    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    anchor = anchors.anchor(["def beta():", "    return 2"])
    out = cmd_def(store, ws, "repo:m.py:beta")
    assert f"definition: repo:m.py L4:5@{anchor}" in out
    assert f"live: ctx get repo:m.py --lines 4:5@{anchor}" in out


def test_symbol_and_an_anchor_cannot_be_combined(state_home, workspace_dir):
    """--symbol resolves its own range, discarding the caller's. Silently
    dropping the anchor with it would leave an address that looks verified and
    is not -- this mechanism's own failure mode, reintroduced through a selector
    combination instead of an edit."""
    _seed(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws, state_home)
    anchor = anchors.anchor(["def beta():", "    return 2"])
    with pytest.raises(RetrievalError) as e:
        get(
            store, ws, "repo:m.py",
            Selector(lines=(1, 2), lines_anchor=anchor, symbol="beta"),
        )
    assert "cannot be combined" in str(e.value)
    # Render settings DO survive the rewrite; only the range-bound anchor does not.
    out = get(store, ws, "repo:m.py", Selector(symbol="beta", hashlines=True))
    assert f"L4:{anchors.line_tag('def beta():')}| def beta():" in out
