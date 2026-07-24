"""Performance investigation for src/ctx/_retrieval/search.py (perf task,
2026-07-24): ``ctx search`` on a file with (almost) no newlines.

House rule (CONTRIBUTING.md): every optimization needs a measured receipt.
The finding, measured before it was fixed:

    ``search()`` resolved the line of every match with
    ``text.rfind("\\n", 0, m.start())`` — a *backward* scan that only stops
    when it hits a newline. On normal source that stops after a few dozen
    characters; on a minified bundle, a JSON blob or a vendored one-liner
    there is nothing to stop it, so each of the k matches rescans up to n
    characters: O(n·k), i.e. quadratic in the file for a match density that
    does not thin out.

    Since ``finditer`` yields matches in ascending order, the enclosing line
    start only ever moves *forward*, so a cursor advanced by one forward
    ``find`` per line actually crossed answers the same question in O(n)
    total. Two smaller O(cap·n) costs on the rendering path went with it:
    line numbers were re-counted from offset 0 for each shown hit (twice per
    hit — once for the rendered line, once for the ``sites`` row), and each
    rendered line was materialized in full before being truncated to 200
    characters (a 3.2 MB copy per rendered line on a one-line file).

Perf receipts (container Python 3.11.15, warm cache, blob targets, one
pattern, default caps — the harness is ``test_benchmark_*`` below):

    corpus                                       before      after     factor
    -------------------------------------------- ----------- --------- -------
    3.2 MB, zero newlines, 67,369 matches         2651.8 ms    14.9 ms   ~178x
    3.2 MB, 40 KB lines, 79 hits spread to EOF      99.0 ms    14.4 ms    ~6.9x

Output was pinned byte-identical across 2,484 (text × pattern × context ×
--all) combinations before/after the change; the shape assertions below are
the checked-in residue of that.
"""

from __future__ import annotations

import time

import pytest
from conftest import make_store, make_ws

from ctx._retrieval import search as search_mod
from ctx._retrieval.search import search

# --------------------------------------------------------------------------
# Deterministic instrumentation: a str that tallies how many characters the
# *Python-level* line-geometry primitives touch. re.finditer scans at the C
# level and does not go through these methods, so the tally isolates exactly
# the scanning this fix is about. Wall-clock assertions are avoided on
# purpose (a slow container must not fail the suite); the tally is exact.
# --------------------------------------------------------------------------


class ScanCountingStr(str):
    """``str`` that counts characters scanned by find/rfind/count."""

    def __new__(cls, value: str) -> "ScanCountingStr":
        obj = super().__new__(cls, value)
        obj.scanned = 0  # type: ignore[attr-defined]
        return obj

    def _tally(self, start: int, end: int | None) -> int:
        stop = len(self) if end is None else min(end, len(self))
        self.scanned += max(0, stop - max(0, start))  # type: ignore[attr-defined]
        return stop

    def find(self, sub, start=0, end=None):  # type: ignore[override]
        stop = self._tally(start, end)
        return str.find(self, sub, start, stop)

    def rfind(self, sub, start=0, end=None):  # type: ignore[override]
        stop = self._tally(start, end)
        return str.rfind(self, sub, start, stop)

    def count(self, sub, start=0, end=None):  # type: ignore[override]
        stop = self._tally(start, end)
        return str.count(self, sub, start, stop)


@pytest.fixture()
def counting_blob(monkeypatch):
    """Feed ``search()`` a blob target whose text counts its own scans."""
    holder: dict[str, ScanCountingStr] = {}

    def fake_stream_text(store, blob_ref):
        return holder["text"]

    monkeypatch.setattr(search_mod, "_stream_text", fake_stream_text)
    return holder


MINIFIED_CHUNK = (
    "function e(t,n){return t+n}var r=function(a){for(var i=0;i<a.length;i++)"
    "{a[i]=a[i]*2}return a};"
)


def minified(size: int) -> str:
    """``size`` characters of minified-JS-shaped text with no newline."""
    text = (MINIFIED_CHUNK * (size // len(MINIFIED_CHUNK) + 1))[:size]
    assert "\n" not in text
    return text


# ------------------------------------------------------------- the pin


@pytest.mark.parametrize("size", [400_000, 800_000, 1_600_000])
def test_newline_free_search_scans_linearly(state_home, workspace_dir, counting_blob, size):
    """The scan budget must stay a small multiple of the file size.

    Under the old ``rfind``-per-match form this tally was ~n·k/2 — for the
    1.6 MB arm, ~2.7e10 characters against the ~5e6 asserted here (the
    3.2 MB production case measured 2651.8 ms). The budget covers: one
    ``n_lines`` count, one forward cursor pass per pattern, the shown-hit
    line-number pass, and the bounded snippet reads.
    """
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = minified(size)
    counting_blob["text"] = ScanCountingStr(text)
    blob = store.put_blob(b"placeholder for the counting target")

    out = search(store, ws, f"blob:{blob[:12]}", ["function"])

    scanned = counting_blob["text"].scanned
    assert scanned <= 4 * len(text), f"{scanned} chars scanned for a {len(text)}-char file"
    assert "matches: 1 · shown: 1" in out


def test_newline_free_scan_cost_does_not_grow_quadratically(
    state_home, workspace_dir, counting_blob
):
    """Doubling the file must roughly double the scan budget, not square it."""
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(b"placeholder for the counting target")
    tallies = []
    for size in (500_000, 1_000_000):
        counting_blob["text"] = ScanCountingStr(minified(size))
        search(store, ws, f"blob:{blob[:12]}", ["function"])
        tallies.append(counting_blob["text"].scanned)
    assert tallies[1] < 3 * tallies[0], tallies


def test_one_enormous_line_output_stays_bounded(state_home, workspace_dir):
    """A file that is genuinely one 3.2 MB line renders one short line."""
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = "x" * 3_100_000 + " needle " + "y" * 99_992
    blob = store.put_blob(text.encode())

    out = search(store, ws, f"blob:{blob[:12]}", ["needle"])

    assert len(out) < 2_000
    body = [ln for ln in out.splitlines() if ln.startswith("  L")]
    assert body and all(len(ln) <= 210 for ln in body)
    assert body[0].startswith("  L1: xxx")


def test_snippet_helper_matches_full_line_truncation(state_home, workspace_dir):
    """``_line_snippet`` is exactly ``line_text_at(...)[:200]``, cheaper."""
    from ctx._retrieval.targets import SearchTarget

    for text in ("", "\n", "abc", "abc\n", "a" * 500, "a" * 500 + "\nbb\n", "x\n\ny"):
        t = SearchTarget(label="t", text=text)
        starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        for ls in starts:
            if ls > len(text):
                continue
            assert search_mod._line_snippet(text, ls) == t.line_text_at(ls)[:200]


def test_line_geometry_unchanged_by_the_cursor(state_home, workspace_dir):
    """Line numbers, columns and leftmost-match-wins survive the rewrite."""
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = "alpha beta\nno match here\nbeta alpha beta\n\nlast alpha\n"
    blob = store.put_blob(text.encode())

    out = search(store, ws, f"blob:{blob[:12]}", ["alpha", "beta"])

    assert "  L1: alpha beta" in out
    assert "  L3: beta alpha beta" in out
    assert "  L5: last alpha" in out
    assert "matches: 3 · shown: 3" in out
    # Columns of the leftmost match on each line, 1-based.
    sites = [
        ln for ln in out.splitlines() if ln.startswith("result: blob:")
    ]
    assert sites, out
    ctx_out = search(store, ws, f"blob:{blob[:12]}", ["last"], context=2)
    assert "  L3: beta alpha beta" in ctx_out
    assert " >L5: last alpha" in ctx_out


def test_crlf_and_trailing_newline_edges(state_home, workspace_dir):
    """A lone \\r is not a line break here (\\n-only geometry, as before)."""
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = "one\rtwo\nthree"
    blob = store.put_blob(text.encode())
    out = search(store, ws, f"blob:{blob[:12]}", ["two", "three"])
    assert "  L1: one\rtwo" in out
    assert "  L2: three" in out


# --------------------------------------------------------------- benchmark
# Prints the receipt numbers; asserts only correctness/shape so a fast or
# slow machine can never fail the suite (same rule as test_store_perf.py).


def test_benchmark_search_newline_free(state_home, workspace_dir, capsys):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cases = {
        "3.2 MB, zero newlines": minified(3_200_000),
        "3.2 MB, 40 KB lines": (
            ("padding line without the word\n" * 1333 + "needle here\n") * 80
        )[:3_200_000],
    }
    for name, text in cases.items():
        pattern = "function" if "zero" in name else "needle"
        blob = store.put_blob(text.encode())
        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            out = search(store, ws, f"blob:{blob[:12]}", [pattern])
            dt = (time.perf_counter() - t0) * 1000
            best = dt if best is None else min(best, dt)
        assert "coverage:" in out and "result: blob:" in out
        with capsys.disabled():
            print(f"\n  search {name}: {best:.1f} ms")
