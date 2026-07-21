"""Acceptance: M-K4 SCIP ingestion (docs/SUBSTRATE.md §M-K4).

Opportunistic, precise cross-references from a real ``index.scip``. The
fixture (`tests/fixtures/scip_sample.scip`) is a committed SCIP index
produced by `scip-python` over a two-file project (`pkg/core.py` defining
`helper`/`use_helper`, `main.py` importing and calling `helper`), so tests
need only the protobuf runtime, not the indexer.

The engine ladder — SCIP (exact) → jedi → ast — is verified end to end:
with an index present, `refs` resolves cross-file and discloses `scip
(exact)`; with none, it degrades to jedi/ast; the runtime absent, SCIP is
skipped entirely (absence costs nothing)."""

from __future__ import annotations

import shutil

import pytest

from conftest import make_store, make_ws

HAS_PROTOBUF = True
try:
    import google.protobuf  # noqa: F401
except Exception:
    HAS_PROTOBUF = False

pytestmark = pytest.mark.skipif(
    not HAS_PROTOBUF, reason="protobuf runtime not installed ([scip] extra)"
)

FIXTURE = "tests/fixtures/scip_sample.scip"


_MAIN_PY = (
    "from pkg.core import helper\n"
    "\n"
    "# helper is the tenant helper described in the docs\n"
    'note = "remember to call helper before commit"\n'
    "\n"
    "\n"
    "def local_shadow():\n"
    '    helper = "shadowed string, not the function"\n'
    "    return helper\n"
    "\n"
    "\n"
    "print(helper(1))\n"
)


def _repo_with_index(workspace_dir):
    """Reproduce the ambiguity fixture's source tree + drop the committed
    index. main.py carries decoys (comment, string, shadowing local) that
    textual matching over-matches but SCIP resolves precisely."""
    import pathlib

    (workspace_dir / "pkg").mkdir()
    (workspace_dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (workspace_dir / "pkg" / "core.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef use_helper():\n"
        "    return helper(41)\n", encoding="utf-8",
    )
    (workspace_dir / "main.py").write_text(_MAIN_PY, encoding="utf-8")
    src = pathlib.Path(__file__).resolve().parent.parent / FIXTURE
    shutil.copy(src, workspace_dir / "index.scip")


# -------------------------------------------------------------- the reader
def test_descriptor_name_extraction():
    from ctx import scip_ingest

    cases = {
        "scip-python python scipproj 0.0.1 `pkg.core`/helper().": "helper",
        "scip-python python python-stdlib 3.11 builtins/print().": "print",
        "scip-python python scipproj 0.0.1 main/__init__:": "__init__",
    }
    for sym, name in cases.items():
        assert scip_ingest.descriptor_name(sym) == name
    assert scip_ingest.descriptor_name("") is None


def test_iter_occurrences_reads_real_index(state_home, workspace_dir):
    from ctx import scip_ingest

    ws = make_ws(workspace_dir)
    _repo_with_index(workspace_dir)
    occs = list(scip_ingest.iter_occurrences(ws.root / "index.scip"))
    assert occs, "expected occurrences from the fixture"
    files = {o.file for o in occs}
    assert {"main.py", "pkg/core.py"} <= files
    # ranges are 1-indexed after conversion
    assert all(o.line >= 1 and o.col_a >= 1 for o in occs)
    # at least one definition of helper is flagged
    assert any(o.name == "helper" and o.is_definition for o in occs)


def test_refs_are_precise_and_cross_file(state_home, workspace_dir):
    from ctx import scip_ingest

    ws = make_ws(workspace_dir)
    _repo_with_index(workspace_dir)
    sites = scip_ingest.refs(ws, "helper")
    coords = {(f, ln) for f, ln, _ in sites}
    # Exactly the real function references — cross-file, exact.
    assert coords == {("pkg/core.py", 1), ("pkg/core.py", 6),
                      ("main.py", 1), ("main.py", 12)}
    # The decoys (comment L3, string L4, shadowing local L8/L9) are NOT
    # matched — the precision jedi/ast textual matching cannot give.
    for decoy in ((("main.py", 3)), ("main.py", 4), ("main.py", 8), ("main.py", 9)):
        assert decoy not in coords


def test_no_index_returns_none_empty_index_returns_list(state_home, workspace_dir):
    from ctx import scip_ingest

    ws = make_ws(workspace_dir)
    # No index at all → None (the ladder-fallthrough signal).
    assert scip_ingest.refs(ws, "helper") is None
    _repo_with_index(workspace_dir)
    # Index present but symbol absent → [] (a definitive empty SCIP answer).
    assert scip_ingest.refs(ws, "no_such_symbol") == []


def test_env_override_locates_index(state_home, workspace_dir, monkeypatch, tmp_path):
    from ctx import scip_ingest

    ws = make_ws(workspace_dir)
    _repo_with_index(workspace_dir)
    moved = tmp_path / "elsewhere.scip"
    shutil.move(str(workspace_dir / "index.scip"), moved)
    assert scip_ingest.find_index(ws) is None
    monkeypatch.setenv("CTX_SCIP_INDEX", str(moved))
    assert scip_ingest.find_index(ws) == moved


# -------------------------------------------------------- the engine ladder
def test_resolve_refs_prefers_scip_and_labels_it(state_home, workspace_dir):
    from ctx import codeverbs

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_index(workspace_dir)
    sites, engine = codeverbs.resolve_refs(store, ws, "helper")
    assert engine == "scip (exact)"
    assert len(sites) >= 4  # the four precise cross-file sites


def test_q_refs_stage_discloses_scip_engine(state_home, workspace_dir):
    from ctx.query import run_query

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_index(workspace_dir)
    out, code = run_query(ws, store, "refs helper --trace")
    assert code == 0
    assert "main.py" in out and "pkg/core.py" in out  # cross-file, precise


def test_code_refs_op_meta_shows_scip(state_home, workspace_dir):
    from ctx.plan_ops import OPS, PlanContext

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_index(workspace_dir)
    out = OPS["code.refs"].fn(PlanContext(ws=ws, store=store), {"symbol": "helper"}, None)
    assert out["meta"]["engine"] == "scip (exact)"


def test_scip_absent_runtime_degrades(state_home, workspace_dir, monkeypatch):
    """With the protobuf runtime unavailable, SCIP is skipped and the
    ladder falls through — absence costs nothing, never errors."""
    from ctx import codeverbs, scip_ingest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _repo_with_index(workspace_dir)
    monkeypatch.setattr(scip_ingest, "_scip_pb2", lambda: None)
    assert scip_ingest.refs(ws, "helper") is None
    # ladder still resolves (via jedi/ast over the real source)
    _sites, engine = codeverbs.resolve_refs(store, ws, "helper")
    assert engine in ("jedi", "ast (textual)")
