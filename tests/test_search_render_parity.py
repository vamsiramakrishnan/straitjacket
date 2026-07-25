"""rg/Python search output parity, made structural (R6).

``search()`` and ``_render_rg_search()`` were two full renderers producing
nearly-but-not-quite the same output. The only thing holding them together
was ``tests/test_v03_libraries.py::test_rg_and_python_engines_agree``, which
compares evidence lines on one small ASCII corpus — a hand-maintained
guarantee.

They now share :func:`_render_search`: both engines build ``RenderRow``s and
one function turns rows into bytes. The header, the ``L<n>:`` coordinates,
the ``>`` marker, the coverage frame, the result blob, the snapshot notes,
the continuation, the emission and the telemetry call all exist once.

The tests below pin (a) that the sharing is real, (b) the line geometry the
convergence put under both engines, and (c) the divergence it closed.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest
from conftest import make_store, make_ws

from ctx._retrieval.search import (
    RenderRow,
    _hit_window,
    _line_starts,
    _render_search,
)
from ctx._retrieval.targets import SearchTarget

HAS_RG = shutil.which("rg") is not None
SRC = Path(__file__).resolve().parent.parent / "src" / "ctx"


# --------------------------------------------------------- structural sharing
def test_both_engines_call_the_one_renderer():
    """Neither engine may grow a second copy of the output format."""
    tree = ast.parse((SRC / "_retrieval" / "search.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("search", "_render_rg_search"):
        calls = {
            getattr(c.func, "id", "")
            for c in ast.walk(fns[name])
            if isinstance(c, ast.Call)
        }
        assert "_render_search" in calls, f"{name} must render through _render_search"

    # …and the format strings live in exactly one function.
    for marker in ("coverage:", "result: blob:", "snapshots:", "patterns: "):
        owners = {
            n.name
            for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and any(
                isinstance(c, ast.Constant) and isinstance(c.value, str) and marker in c.value
                for c in ast.walk(n)
            )
        }
        assert owners == {"_render_search"}, (marker, owners)


# ------------------------------------------------------------- line geometry
@pytest.mark.parametrize(
    "text",
    [
        "",
        "a",
        "a\n",
        "a\nb\nc\n",
        "a\nb\nc",
        "\n\n\n",
        "a\n\nb\n",
        "x" * 5000,
        ("y" * 300 + "\n") * 5,
        "one\rtwo\nthree",
        "one two\nthree",
        "one\x0btwo\nthree",
    ],
)
def test_line_starts_inverts_line_numbers(text):
    """``_line_starts`` (line number → offset) must be the exact inverse of
    the offset → line number pass the Python engine already used."""
    t = SearchTarget(label="t", text=text)
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    starts = [s for s in starts if s <= len(text)]
    line_nos = [t.line_no_of(s) for s in starts]
    got = _line_starts(text, sorted(set(line_nos)))
    for n, s in zip(line_nos, starts):
        assert got[n] == s, (text, n, got[n], s)


def test_line_starts_is_newline_only():
    """``str.splitlines()`` also breaks on \\r, \\v, \\f, \\x1c, \\x85,
    \\u2028 and \\u2029. Ripgrep numbers lines by \\n, and so does the Python
    engine (``tests/test_search_perf.py`` pins the \\r case), so the shared
    geometry must too — otherwise the coordinate and the text disagree."""
    text = "a\rb\x0bc\x0cd\x1ce\x85f g h\nsecond\n"
    assert len(text.splitlines()) == 9
    assert _line_starts(text, [1, 2]) == {1: 0, 2: text.index("\n") + 1}


def test_hit_window_matches_a_naive_reference():
    """The shared window against an obvious \\n-split implementation."""
    text = "l1\nl2\nl3\nl4\nl5\nl6\nl7\n"
    t = SearchTarget(label="t", text=text)
    lines = text.split("\n")[:-1]
    starts = _line_starts(text, list(range(1, len(lines) + 1)))
    for n in range(1, len(lines) + 1):
        for context in (0, 1, 2, 3, 10):
            before, hit, after = _hit_window(t, starts[n], context)
            lo = max(0, n - 1 - context)
            hi = min(len(lines), n + context)
            assert hit == lines[n - 1]
            assert list(before) == lines[lo : n - 1]
            assert list(after) == lines[n:hi]


def test_hit_window_never_materializes_a_giant_line():
    """The bounded-extraction property the Python path already had, now on
    both: a 3 MB single line yields a short string, not a 3 MB copy."""
    text = "z" * 3_000_000
    t = SearchTarget(label="t", text=text)
    before, hit, after = _hit_window(t, 0, 3)
    assert len(hit) == 200 and not before and not after


# ------------------------------------------------------------ the marker rule
def test_the_marker_tracks_the_request_not_the_neighbours():
    """``--context N`` on the only line of a file has no neighbours to show
    and still marks the hit — the pre-convergence Python engine's behaviour,
    which a naive ``before or after`` test would have silently dropped."""
    row = RenderRow(target="a.txt", line_no=1, col_a=1, col_b=2, text="only",
                    contextual=True)
    assert row.before == () and row.after == ()


def test_context_on_a_one_line_file_still_marks(state_home, workspace_dir, monkeypatch):
    from ctx.retrieval import search

    monkeypatch.setenv("CTX_SEARCH_ENGINE", "python")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "a.txt").write_text("only needle here\n", encoding="utf-8")
    out = search(store, ws, "repo:", ["needle"], context=3)
    assert " >L1: only needle here" in out


# ---------------------------------------------------- the divergence it closed
_EXOTIC = {
    "bare_cr": "one needle\rtwo needle\nthree\nfour needle\n",
    "vertical_tab": "one needle\x0btwo needle\nthree needle\n",
    "form_feed": "one needle\x0ctwo needle\nthree needle\n",
    "file_sep": "one needle\x1ctwo needle\nthree needle\n",
    "next_line": "one needle\x85two needle\nthree needle\n",
    "line_sep": "one needle two needle\nthree needle\n",
    "para_sep": "one needle two needle\nthree needle\n",
}


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
@pytest.mark.parametrize("name", sorted(_EXOTIC))
def test_engines_agree_on_files_with_exotic_line_separators(
    name, state_home, tmp_path, monkeypatch
):
    """The rg engine used to read context with ``splitlines()`` while
    printing ripgrep's ``\\n``-based line numbers beside it, so on any file
    containing one of these characters the coordinate addressed one line and
    the text showed another. Both engines now use one geometry."""
    from ctx.retrieval import search

    def run(engine: str) -> list[str]:
        root = tmp_path / f"{name}-{engine}"
        root.mkdir()
        (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
        (root / "a.txt").write_text(_EXOTIC[name], encoding="utf-8", newline="")
        ws = make_ws(root)
        store = make_store(ws)
        if engine == "python":
            monkeypatch.setenv("CTX_SEARCH_ENGINE", "python")
        else:
            monkeypatch.delenv("CTX_SEARCH_ENGINE", raising=False)
        out = search(store, ws, "repo:", ["needle"], context=1)
        return [ln for ln in out.splitlines() if ln.startswith((" >L", "  L"))]

    assert run("rg") == run("python")


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_the_coordinate_and_the_text_agree(state_home, tmp_path, monkeypatch):
    """The concrete regression, spelled out: with a bare ``\\r`` on line 1,
    ripgrep's third line is ``four needle`` and that is what must print."""
    from ctx.retrieval import search

    monkeypatch.delenv("CTX_SEARCH_ENGINE", raising=False)
    root = tmp_path / "cr"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "a.txt").write_text(_EXOTIC["bare_cr"], encoding="utf-8", newline="")
    ws = make_ws(root)
    store = make_store(ws)
    out = search(store, ws, "repo:", ["needle"], context=1)
    assert " >L3: four needle" in out
    assert " >L3: three" not in out


# ------------------------------------------------------------- the shared tail
def test_renderer_emits_the_whole_frame(state_home, workspace_dir):
    """One call site for header, body, coverage, provenance and snapshots."""
    from ctx.refs import parse_ref

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "a.txt").write_text("hello\n", encoding="utf-8")
    rows = [RenderRow(target="a.txt", line_no=1, col_a=1, col_b=6, text="hello")]
    out = _render_search(
        store, ws, parse_ref("repo:"), "repo:", ["hello"], rows,
        mode_all=False, total=1, scanned="  scanned: bespoke", cap=10,
        snapshots=True, telemetry_bytes=6,
    )
    assert out.splitlines()[0].startswith("[ctx search repo:")
    assert "patterns: 'hello' (any)" in out
    assert "a.txt:" in out
    assert "  L1: hello" in out
    assert "coverage:" in out and "  scanned: bespoke" in out
    assert "  matches: 1 · shown: 1" in out
    assert "result: blob:" in out
    assert "snapshots:" in out


def test_truncation_note_and_continuation_are_shared(state_home, workspace_dir):
    from ctx.refs import parse_ref

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    rows = [RenderRow(target="a.txt", line_no=1, col_a=1, col_b=2, text="x")]
    out = _render_search(
        store, ws, parse_ref("repo:"), "repo:", ["x"], rows,
        mode_all=True, total=9, scanned="  scanned: s", cap=4,
        snapshots=False, telemetry_bytes=1,
    )
    assert "patterns: 'x' (all)" in out
    assert "  matches: 9 · shown: 1 · truncated" in out
    assert "--max-matches 8" in out
    assert "snapshots:" not in out
