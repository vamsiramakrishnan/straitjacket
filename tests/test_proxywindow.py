"""One fail-open reader for the proxy's ``window.json`` (R3).

Six planes read this file; each carried its own ``open`` + ``json.load`` +
bare ``except``. They agreed, but the fail-open contract they all depend on
was maintained by hand in six places. These tests pin the contract itself,
including every degradation the inline copies handled implicitly, and check
that each caller still extracts what it always did.
"""

from __future__ import annotations

import json
import os

import pytest

from ctx.proxywindow import (
    PROXY_SUBDIR,
    WINDOW_FILENAME,
    read_window_doc,
    window_path,
)
from ctx.sessiondir import session_reads_dir


def _write(ws, body: str) -> None:
    d = ws / ".ctx-session-reads" / PROXY_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    (d / WINDOW_FILENAME).write_text(body, encoding="utf-8")


# --------------------------------------------------------------- the path
def test_path_is_the_ledger_directory_subpath():
    assert window_path("/ws") == session_reads_dir("/ws", "proxy", "window.json")


def test_writer_and_readers_share_the_filename():
    """ctx.proxy is the sole writer; if it renamed the file, every reader
    would silently see 'no proxy' forever. Same constant, both ends."""
    from ctx import proxy

    assert proxy.WINDOW_FILENAME is WINDOW_FILENAME


# ----------------------------------------------------- the fail-open contract
def test_absence_is_normal_not_an_error(tmp_path):
    """A plain session with no proxy never creates this file. That is the
    common case, not a failure — it must read as an empty document."""
    assert read_window_doc(tmp_path) == {}


def test_missing_ledger_directory(tmp_path):
    assert read_window_doc(tmp_path / "nope") == {}


@pytest.mark.parametrize("root", [None, "", 0])
def test_falsy_workspace_root(root):
    """Several callers guarded this themselves before touching the path."""
    assert read_window_doc(root) == {}


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "not json at all",
        '{"window_pct": 84.5',  # truncated: a half-written snapshot
        "﻿{}",  # BOM
    ],
)
def test_malformed_json_reads_as_empty(tmp_path, body):
    _write(tmp_path, body)
    assert read_window_doc(tmp_path) == {}


@pytest.mark.parametrize("body", ["[1,2,3]", '"a bare string"', "null", "42", "true"])
def test_valid_json_that_is_not_an_object_reads_as_empty(tmp_path, body):
    """The inline copies each got this for free from ``.get`` raising into
    their bare ``except``; here it is the reader's stated job."""
    _write(tmp_path, body)
    assert read_window_doc(tmp_path) == {}


def test_a_directory_where_the_file_should_be(tmp_path):
    d = tmp_path / ".ctx-session-reads" / PROXY_SUBDIR / WINDOW_FILENAME
    d.mkdir(parents=True)
    assert read_window_doc(tmp_path) == {}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file(tmp_path):
    _write(tmp_path, "{}")
    p = tmp_path / ".ctx-session-reads" / PROXY_SUBDIR / WINDOW_FILENAME
    p.chmod(0o000)
    try:
        assert read_window_doc(tmp_path) == {}
    finally:
        p.chmod(0o644)


def test_a_well_formed_document_comes_back_whole(tmp_path):
    doc = {
        "window_pct": 84.5,
        "model": "claude-x",
        "context_limit": 200000,
        "cum_output": 45000,
        "requests": 30,
        "contained_tokens": 1234,
    }
    _write(tmp_path, json.dumps(doc))
    assert read_window_doc(tmp_path) == doc


def test_values_are_not_coerced(tmp_path):
    """Type checking belongs to each caller — the document is written by
    another process and a key may be present but nonsense."""
    _write(tmp_path, '{"window_pct": "nope", "model": 7}')
    assert read_window_doc(tmp_path) == {"window_pct": "nope", "model": 7}


def test_never_raises_on_a_root_that_is_a_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    assert read_window_doc(f) == {}


# --------------------------------------------------- the callers still agree
def test_every_caller_reads_the_one_file(tmp_path):
    from ctx import engagement, hook, resolver, statusline

    _write(
        tmp_path,
        json.dumps(
            {
                "window_pct": 84.5,
                "model": "claude-haiku-4",
                "context_limit": 200000,
                "cum_output": 45000,
                "requests": 30,
                "contained_tokens": 1234,
            }
        ),
    )
    ws = str(tmp_path)
    assert hook._window_pct(ws) == 84.5
    assert resolver.read_window(ws) == (84.5, "claude-haiku-4")
    assert engagement.session_model(ws) == "claude-haiku-4"
    assert engagement.is_lean_model(ws) is True
    assert "% of window" in hook._price_note(100000, ws)
    assert statusline._harness_saved(ws) == "1K kept out"


def test_every_caller_degrades_the_same_way_when_it_is_absent(tmp_path):
    from ctx import engagement, hook, resolver, statusline

    ws = str(tmp_path)
    assert hook._window_pct(ws) is None
    assert resolver.read_window(ws) == (None, None)
    assert engagement.session_model(ws) == ""
    assert engagement.is_lean_model(ws) is False
    assert "% of window" not in hook._price_note(100000, ws)
    assert statusline._harness_saved(ws) is None
    assert hook._emission_nudge({"workspacePaths": [ws], "session_id": "s"}) is None


def test_resolver_still_has_no_import_edge_into_the_hook():
    """The reader used to be replicated in ctx.resolver *specifically* so
    that module would not import the safety plane. Sharing it must not have
    quietly created that edge."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "ctx" / "resolver.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    hits = [
        n
        for n in ast.walk(tree)
        if (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("ctx.hook"))
        or (isinstance(n, ast.Import) and any(a.name.startswith("ctx.hook") for a in n.names))
    ]
    assert not hits
