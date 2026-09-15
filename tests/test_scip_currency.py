"""Per-file currency of the SCIP tier: an index that recorded what each
file contained keeps answering exactly for the files that did not change,
and hands the changed ones to the textual rung, disclosed — instead of
being discarded whole by the first edit."""

from __future__ import annotations

import json
import shutil

import pytest

from conftest import make_store, make_ws
from test_scip_ingest import _MAIN_PY, FIXTURE  # noqa: F401

HAS_PROTOBUF = True
try:
    import google.protobuf  # noqa: F401
except Exception:
    HAS_PROTOBUF = False

pytestmark = pytest.mark.skipif(
    not HAS_PROTOBUF, reason="protobuf runtime not installed ([scip] extra)"
)


def _repo_with_basis(workspace_dir, ws, store):
    """The ambiguity fixture plus the sidecar `ctx index` writes: per-file
    content hashes, so the index knows what it described."""
    import pathlib

    from ctx.scip_ingest import _SIDECAR_NAME, _source_state, content_hashes

    (workspace_dir / "pkg").mkdir()
    (workspace_dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (workspace_dir / "pkg" / "core.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef use_helper():\n"
        "    return helper(41)\n", encoding="utf-8",
    )
    (workspace_dir / "main.py").write_text(_MAIN_PY, encoding="utf-8")
    src = pathlib.Path(__file__).resolve().parent.parent / FIXTURE
    shutil.copy(src, workspace_dir / "index.scip")
    count, newest = _source_state(ws)
    (workspace_dir / _SIDECAR_NAME).write_text(json.dumps({
        "language": "python", "files": count, "max_mtime_ns": newest,
        "files_sha": content_hashes(ws, store),
    }), encoding="utf-8")


def test_a_changed_file_is_answered_textually_and_the_rest_exactly(state_home, workspace_dir):
    from ctx.codeverbs import resolve_refs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_basis(workspace_dir, ws, store)
    sites, label = resolve_refs(store, ws, "helper")
    assert label == "scip (exact)"

    # a new call site in a file that changed after indexing
    p = workspace_dir / "main.py"
    p.write_text(p.read_text(encoding="utf-8") + "\nhelper(2)\n", encoding="utf-8")
    sites, label = resolve_refs(store, ws, "helper")
    assert label == "scip (exact) · 1 changed file via ast (textual)"
    coords = {(f, ln) for f, ln, _ in sites}
    # exact sites from the untouched file, and the new site from the changed one
    assert ("pkg/core.py", 1) in coords and ("pkg/core.py", 6) in coords
    assert ("main.py", 14) in coords
    # the textual rung's decoys in the changed file come along (it is textual,
    # and the label says so); nothing from the unchanged file is textual
    assert ("main.py", 12) in coords


def test_a_new_file_counts_as_changed(state_home, workspace_dir):
    from ctx import scip_ingest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_basis(workspace_dir, ws, store)
    (workspace_dir / "pkg" / "extra.py").write_text("from pkg.core import helper\nhelper(3)\n",
                                                   encoding="utf-8")
    index = scip_ingest.find_index(ws, store)
    assert scip_ingest.stale_files(ws, index, store) == ["pkg/extra.py"]
    got = scip_ingest.refs_partial(ws, "helper", store=store)
    assert got is not None
    sites, stale = got
    assert stale == ["pkg/extra.py"] and all(f != "pkg/extra.py" for f, _, _ in sites)


def test_no_basis_means_no_partial_answer(state_home, workspace_dir):
    """The committed fixture has no sidecar: the all-or-nothing rule stands."""
    from ctx import scip_ingest
    from test_scip_ingest import _repo_with_index

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_index(workspace_dir)
    assert scip_ingest.refs_partial(ws, "helper", store=store) is None
    assert scip_ingest.stale_files(ws, scip_ingest.find_index(ws, store), store) is None


def test_def_reaches_the_exact_tier_through_the_partial_index(state_home, workspace_dir):
    from ctx.codeverbs import cmd_def

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_basis(workspace_dir, ws, store)
    p = workspace_dir / "main.py"
    p.write_text(p.read_text(encoding="utf-8") + "\nhelper(2)\n", encoding="utf-8")
    out = cmd_def(store, ws, "repo:pkg/core.py:helper")
    assert "scip" in out.splitlines()[0]


def test_index_status_reports_the_changed_files(state_home, workspace_dir, capsys):
    from ctx.cli import main

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_basis(workspace_dir, ws, store)
    p = workspace_dir / "main.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n# note\n", encoding="utf-8")
    assert main(["--workspace", str(workspace_dir), "index", "--status"]) == 0
    out = capsys.readouterr().out
    assert "scip: present · 1 file(s) changed since indexing" in out


def test_implementations_come_from_relationship_edges(state_home, workspace_dir):
    """The fixture has no subclasses, so the exact answer is an empty list
    with the engine disclosed — and not a fall-through to the call graph."""
    from ctx import scip_ingest
    from ctx.commands.retrieve import _scip_impls

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_basis(workspace_dir, ws, store)
    got = scip_ingest.implementations(ws, "helper", store=store)
    assert got == ([], [])
    out = _scip_impls(store, ws, "helper")
    assert out is not None and "engine scip (exact)" in out and "implementations: 0" in out
