"""M-C acceptance: ranked, budget-fitted, cached, evidence-weighted repo map."""

import re
import sys

import pytest
from conftest import make_store, make_ws

try:
    import grimp  # noqa: F401
    import networkx  # noqa: F401

    _HAVE_MAP_ENGINE = True
except ImportError:
    _HAVE_MAP_ENGINE = False

needs_engine = pytest.mark.skipif(
    not _HAVE_MAP_ENGINE, reason="optional map engine (grimp+networkx) not installed"
)

IMPORTERS = ["alpha", "beta", "delta", "echo", "foxtrot", "golf", "india", "juliet", "kilo"]


def _build_pkg(root):
    """Synthetic 12-file package: pkg/core.py imported by 9 modules."""
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "def connect(host, port):\n    return (host, port)\n\n\n"
        "class Engine:\n    def start(self):\n        return True\n",
        encoding="utf-8",
    )
    (pkg / "hot.py").write_text("def boil(temp):\n    return temp\n", encoding="utf-8")
    for name in IMPORTERS:
        (pkg / f"{name}.py").write_text(
            f"from pkg.core import connect\n\n\n"
            f"def use_{name}(first_argument, second_argument):\n"
            f"    return connect(first_argument, second_argument)\n",
            encoding="utf-8",
        )


def _map(ws, store, **kw):
    from ctx.repomap import repo_map

    return repo_map(store, ws, **kw)


def _file_lines(m: str) -> list[str]:
    return [ln for ln in m.splitlines() if ln.startswith("repo:")]


def _omitted(m: str) -> tuple[int, int]:
    match = re.search(r"omitted: ([\d,]+) files · ([\d,]+) symbols", m)
    assert match, m
    return int(match.group(1).replace(",", "")), int(match.group(2).replace(",", ""))


# ------------------------------------------------------------------ ranking
def test_most_imported_module_ranks_first(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    m = _map(ws, make_store(ws), budget=1200)
    assert m.startswith("[ctx map 12 files · budget 1200 tok · engine builtin]")
    files = _file_lines(m)
    assert files[0].startswith("repo:pkg/core.py")
    assert "imported-by 9" in files[0]


# ------------------------------------------------ determinism + cache round-trip
def test_byte_identical_and_cached(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    m1 = _map(ws, store, budget=600)
    maps_dir = store.root / "indexes" / "maps"
    assert len(list(maps_dir.iterdir())) == 1  # rendered map cached by key
    m2 = _map(ws, store, budget=600)
    assert m1 == m2
    # Store round-trip: fresh Workspace + Store over the same state.
    ws2 = make_ws(workspace_dir)
    m3 = _map(ws2, make_store(ws2), budget=600)
    assert m1 == m3


# ------------------------------------------------------------------- budgets
def test_budget_compliance_and_declared_omission(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    m200 = _map(ws, store, budget=200)
    m1200 = _map(ws, store, budget=1200)
    assert len(m200.encode("utf-8")) // 4 <= 200 + 2
    assert len(m1200.encode("utf-8")) // 4 <= 1200 + 2
    k, _ = _omitted(m200)
    assert k > 0  # omission is declared, never silent
    assert _omitted(m1200) == (0, 0)
    assert len(m200) < len(m1200)


# --------------------------------------------------------------------- focus
def test_focus_lifts_named_file(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cold = _map(ws, store, budget=1200)
    assert not _file_lines(cold)[0].startswith("repo:pkg/juliet.py")
    focused = _map(ws, store, budget=1200, focus="juliet")
    assert _file_lines(focused)[0].startswith("repo:pkg/juliet.py")
    assert focused == _map(ws, store, budget=1200, focus="juliet")  # still deterministic


# ------------------------------------------------------ addressable symbols
def test_symbols_resolve_via_existing_get_selector(state_home, workspace_dir, monkeypatch):
    from ctx.retrieval import Selector, get

    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    m = _map(ws, store, budget=1200)
    assert "repo:pkg/core.py --symbol connect · (host, port)" in m
    assert "repo:pkg/core.py --symbol Engine · class" in m
    out = get(store, ws, "repo:pkg/core.py", Selector(symbol="connect"))
    assert "def connect" in out and "return (host, port)" in out


# --------------------------------------------------------- evidence weighting
def test_recent_run_evidence_boosts_mentioned_file(state_home, workspace_dir, monkeypatch):
    from ctx.execution import run_capture

    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cold = _map(ws, store, budget=1200)
    assert "hot (recent run evidence):" not in cold  # empty store: skipped silently

    run_capture(
        ws,
        [sys.executable, "-c", "print('FAILED tests touching pkg/hot.py')"],
        store=store,
    )
    hot = _map(ws, store, budget=1200)
    assert "hot (recent run evidence):" in hot
    assert "  pkg/hot.py" in hot
    def _rank(m: str) -> int:
        return next(
            i for i, ln in enumerate(_file_lines(m)) if ln.startswith("repo:pkg/hot.py")
        )

    cold_rank = _rank(cold)
    hot_rank = _rank(hot)
    assert hot_rank < cold_rank


# ------------------------------------------------------------ ctags fallback
def test_ctx_no_ctags_forces_python_only_map(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    (workspace_dir / "pkg" / "web.js").write_text(
        "function init(a, b) { return a + b; }\n", encoding="utf-8"
    )
    ws = make_ws(workspace_dir)
    m = _map(ws, make_store(ws), budget=1200)
    assert "web.js" not in m  # transparent fallback: non-Python files omitted
    assert m.startswith("[ctx map 12 files")


# ------------------------------------------------------------------- wiring
def test_cli_map_subcommand(state_home, workspace_dir, monkeypatch, capsys):
    from ctx.cli import main

    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    rc = main(["--workspace", str(workspace_dir), "map", "--budget", "300", "--focus", "hot"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("[ctx map 12 files · budget 300 tok · engine builtin]")
    assert _file_lines(out)[0].startswith("repo:pkg/hot.py")


def test_mcp_map_op(state_home, workspace_dir, monkeypatch):
    from ctx.mcp import TOOL_SCHEMA, _dispatch

    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    _build_pkg(workspace_dir)
    assert "map" in TOOL_SCHEMA["inputSchema"]["properties"]["op"]["enum"]
    out = _dispatch({"op": "map", "workspace": str(workspace_dir), "options": {"budget": 300}})
    assert out.startswith("[ctx map 12 files · budget 300 tok · engine builtin]")
    assert "omitted:" in out


# ---------------------------------------------------- engine: grimp+networkx
@needs_engine
def test_grimp_engine_ranks_most_imported_first(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.delenv("CTX_MAP_ENGINE", raising=False)
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    m = _map(ws, make_store(ws), budget=1200)
    assert m.startswith("[ctx map 12 files · budget 1200 tok · engine grimp+networkx]")
    files = _file_lines(m)
    assert files[0].startswith("repo:pkg/core.py")
    assert "imported-by 9" in files[0]


@needs_engine
def test_grimp_engine_byte_identical_across_calls(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    monkeypatch.delenv("CTX_MAP_ENGINE", raising=False)
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    m1 = _map(ws, store, budget=600)
    m2 = _map(ws, store, budget=600)
    assert m1 == m2
    # Fresh Workspace + Store over the same state: still byte-identical.
    ws2 = make_ws(workspace_dir)
    m3 = _map(ws2, make_store(ws2), budget=600)
    assert m1 == m3


@needs_engine
def test_engine_header_discloses_selection(state_home, workspace_dir, monkeypatch):
    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    forced = _map(ws, store, budget=1200)
    assert "· engine builtin]" in forced.splitlines()[0]
    monkeypatch.delenv("CTX_MAP_ENGINE", raising=False)
    auto = _map(ws, store, budget=1200)
    assert "· engine grimp+networkx]" in auto.splitlines()[0]


@needs_engine
def test_both_engines_resolve_symbols_via_get(state_home, workspace_dir, monkeypatch):
    from ctx.retrieval import Selector, get

    monkeypatch.setenv("CTX_NO_CTAGS", "1")
    _build_pkg(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    monkeypatch.setenv("CTX_MAP_ENGINE", "builtin")
    builtin_map = _map(ws, store, budget=1200)
    monkeypatch.delenv("CTX_MAP_ENGINE", raising=False)
    grimp_map = _map(ws, store, budget=1200)
    # The maps may differ (different resolvers/rankers), but every emitted
    # symbol line stays addressable through the shared get selector.
    for m in (builtin_map, grimp_map):
        assert "repo:pkg/core.py --symbol connect · (host, port)" in m
        assert "repo:pkg/core.py --symbol Engine · class" in m
    out = get(store, ws, "repo:pkg/core.py", Selector(symbol="connect"))
    assert "def connect" in out and "return (host, port)" in out
