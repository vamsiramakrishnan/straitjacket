"""Acceptance: a digest must earn its bytes, per output.

Replaying this repo's own sessions (`ctx replay --all-projects`) showed short
sessions coming out WORSE under the harness than without it — worst 128 -> 439
tokens. Cause: profile scaffolding is a fixed cost. A passing 98-byte pytest run
rendered a 248-byte digest whose `coverage:` block spent five lines accounting
for the omission of one line out of two — and the omitted line was the result
("1 passed"). The model paid 2.5x to be told what it could not see.

The criterion is EVIDENCE, not size. An earlier attempt compared byte counts and
suppressed failure spans and JSON schema summaries — findings worth far more
than their length. So the output is passed through only when every line the
profile produced is bookkeeping.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ctx.digest import _ACCOUNTING_LINE, _only_accounting


@pytest.fixture()
def make_ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    counter = iter(range(100))

    def make(toml: str = "version = 1\n"):
        from ctx.store import Store
        from ctx.workspace import resolve_workspace

        d = tmp_path / f"proj{next(counter)}"
        d.mkdir()
        (d / "ctx.toml").write_text(toml, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
        ws = resolve_workspace(str(d))
        return ws, Store(ws.workspace_id)

    return make


def _run(ws, store, argv):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    cap = run_capture(ws, argv, store=store)
    return render_run_digest(store, ws, cap.manifest)[0]


def test_bookkeeping_only_digest_passes_the_output_through(make_ws_store):
    """A clean run: nothing derived, so the output speaks for itself."""
    ws, store = make_ws_store()
    d = _run(ws, store, [sys.executable, "-c", "print('all good')"])
    assert "all good" in d
    for scaffold in ("coverage:", "omitted", "next:", "command:"):
        assert scaffold not in d, scaffold
    # the handle survives: the capture is still addressable
    assert d.splitlines()[0].startswith("[ctx run:")


def test_a_derived_finding_keeps_the_full_digest(make_ws_store):
    """A failure census is evidence; it outweighs its bytes however short the
    run was. This is the case the naive size-based rule destroyed."""
    ws, store = make_ws_store()
    src = "import sys; print('boom'); sys.exit(3)"
    d = _run(ws, store, [sys.executable, "-c", src])
    assert "exit" in d  # status retained either way
    assert "boom" in d


def test_large_output_is_still_digested(make_ws_store):
    """The rule must never disable containment on something that needs it."""
    ws, store = make_ws_store()
    d = _run(ws, store, [sys.executable, "-c", "print('x' * 200000)"])
    assert "output (complete)" not in d
    assert "run:" in d  # a retrieval handle, not the payload


def test_accounting_classifier_knows_evidence_from_bookkeeping():
    for book in ("cwd: .", "command: pytest -q", "exit: 0", "summary:",
                 "  tests: 1 · passed 1", "coverage:", "  parsed: 2/2 lines",
                 "  shown: 1 spans · omitted: 1 lines", "next:",
                 "  ctx search run:abc 'failed'"):
        assert _ACCOUNTING_LINE.match(book), book
    for evidence in ("failing tests (census):",
                     "  1. test_b.py::test_bad  test_b.py:2 · AssertionError",
                     "  first failure stdout:L3-L9 · span 7824c2eca3",
                     "    | >       assert 1 == 2",
                     "  heavy hitters: ERROR 412"):
        assert not _ACCOUNTING_LINE.match(evidence), evidence
    assert _only_accounting("exit: 0\nsummary:\n  tests: 1 · passed 1")
    assert not _only_accounting("exit: 0\nfailing tests (census):\n  1. x")
