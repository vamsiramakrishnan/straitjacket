"""Acceptance: mcp.py surface drift fixes (docs/LADDERS.md edge 3) and the
bounded workspace cache (S6 finding).

1. The TOOL_SCHEMA ``op`` enum must declare every op ``_dispatch`` actually
   handles (callers/callees/impact were dispatched but undeclared; diff was
   neither declared nor dispatched) — a model reading the schema must be
   able to discover every reachable op.
2. Conversely, every declared op must be *described* in the tool description
   — the enum grew to 14 ops while the prose catalogue still listed 9, so
   five ops were callable but undiscoverable (D1).
3. ``_WS_CACHE`` must never grow without bound and must close evicted Store
   sqlite connections rather than leaking them.

The MCP tool description string is a golden-hashed prefix asset
(``ctx.prefixassets``): editing it is an explicit, versioned decision that
costs one cold cache write per model, so it moves together with
PREFIX_VERSION and the committed manifest — ``test_prefix_stability.py`` is
the guard for that invariant.
"""

import subprocess
import sys

import pytest

from conftest import make_store, make_ws

CALLGRAPH_SRC = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "def leaf():\n"
        "    return 1\n\n\n"
        "def mid():\n"
        "    return leaf() + leaf()\n"
    ),
}

PASSING = "========================= test session starts ==========================\n2 passed\n"
FAILING = "========================= test session starts ==========================\n1 failed, 1 passed\n"


@pytest.fixture()
def callgraph_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel, content in CALLGRAPH_SRC.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    return d


# --------------------------------------------------------------- op enum


def test_tool_schema_enum_declares_every_dispatched_op():
    from ctx.mcp import TOOL_SCHEMA

    enum = TOOL_SCHEMA["inputSchema"]["properties"]["op"]["enum"]
    for op in ("callers", "callees", "impact", "diff"):
        assert op in enum, f"{op!r} is dispatched but missing from the op enum"


def test_tool_description_documents_every_enum_op():
    """Drift guard: the tool description is the only op catalogue a model
    reads before it picks an op. Every declared op must be glossed there, or
    the op is callable-but-undiscoverable (callers/callees/impact/diff/
    investigate were exactly that). Naming an op in the enum is therefore a
    commitment to describe it — with a parenthetical gloss, not a bare name,
    so a model can choose between ops without trial calls."""
    from ctx.mcp import TOOL_SCHEMA

    description = str(TOOL_SCHEMA["description"])
    enum = TOOL_SCHEMA["inputSchema"]["properties"]["op"]["enum"]
    for op in enum:
        assert f"{op} (" in description, (
            f"op {op!r} is in the enum but has no gloss in the tool description; "
            "add one (and bump PREFIX_VERSION — the description is a prefix asset)"
        )


def test_tool_description_bytes_unchanged():
    """The golden-hashed asset is TOOL_SCHEMA['description'] specifically
    (see ctx.prefixassets.prefix_assets); enum/schema extensions must not
    touch it, or PREFIX_VERSION must move. This pins the exact text so an
    accidental edit fails loudly here, not just in the manifest test."""
    from ctx.mcp import TOOL_SCHEMA

    assert TOOL_SCHEMA["description"] == (
        'Execute bounded retrieval against repository state or captured artifacts without placing unbounded output in model context. Ops: search (multi-pattern over run:/blob:/repo: refs), get (exact line/byte/record/json-pointer slices), stats (schema and repository shape), map (ranked budget-fitted codebase map), def (symbol definition site with snapshot + span), refs (reference sites for a symbol), diag (deterministic lint/syntax digest), callers (direct call-graph callers of a symbol), callees (direct call-graph callees of a symbol), impact (transitive callers of a symbol — blast radius, bounded depth), diff (regression delta between two captured run: refs), repo (workspace summary), doctor (health), investigate (one observe-class ctx.plan/v1 evidence plan executed as a single bounded digest), q (compose typed evidence in one call: a total `|`-pipeline over symbols/sites/files/records streams — options.pipeline, e.g. "refs Foo | group file | top 3 | get --context 5"). task (the collaboration ledger for options.task — claims, handbacks, steward decisions, inbox — or the task list when omitted), inbox (addresses handed to options.node in options.task), send (hand options.node an ADDRESS — options.ref, never content — with an optional bounded options.note).'
    )


def test_mcp_dispatch_callers_callees_impact(callgraph_workspace):
    from ctx.mcp import _dispatch

    out_callers = _dispatch(
        {"op": "callers", "workspace": str(callgraph_workspace), "options": {"symbol": "leaf"}}
    )
    assert "callers" in out_callers and "mid" in out_callers

    out_callees = _dispatch(
        {"op": "callees", "workspace": str(callgraph_workspace), "options": {"symbol": "mid"}}
    )
    assert "callees" in out_callees and "leaf" in out_callees

    out_impact = _dispatch(
        {"op": "impact", "workspace": str(callgraph_workspace), "options": {"symbol": "leaf", "depth": 3}}
    )
    assert "impact" in out_impact


def test_mcp_dispatch_impact_requires_symbol(callgraph_workspace):
    from ctx.mcp import _dispatch
    from ctx.retrieval import RetrievalError

    with pytest.raises(RetrievalError, match="requires options.symbol"):
        _dispatch({"op": "impact", "workspace": str(callgraph_workspace), "options": {}})


# --------------------------------------------------------------------- diff


def _capture_text(tmp_path, ws, store, name, text, code=0):
    from ctx.execution import run_capture

    payload = tmp_path / name
    payload.write_text(text, encoding="utf-8")
    script = "import sys;sys.stdout.write(open(sys.argv[1]).read());sys.exit(int(sys.argv[2]))"
    return run_capture(
        ws, [sys.executable, "-c", script, str(payload), str(code)], store=store
    )


def test_mcp_dispatch_diff(state_home, workspace_dir, tmp_path):
    from ctx.mcp import _dispatch

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = _capture_text(tmp_path, ws, store, "a.txt", PASSING, code=0)
    b = _capture_text(tmp_path, ws, store, "b.txt", FAILING, code=1)

    out = _dispatch(
        {
            "op": "diff",
            "workspace": str(workspace_dir),
            "options": {"refA": f"run:{a.manifest_id[:12]}", "refB": f"run:{b.manifest_id[:12]}"},
        }
    )
    assert out.startswith("[ctx diff run:")
    assert "exit 0 → 1" in out


def test_mcp_dispatch_diff_requires_both_refs(state_home, workspace_dir, tmp_path):
    from ctx.mcp import _dispatch
    from ctx.retrieval import RetrievalError

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = _capture_text(tmp_path, ws, store, "a.txt", PASSING, code=0)

    with pytest.raises(RetrievalError, match="requires options.refA and options.refB"):
        _dispatch(
            {
                "op": "diff",
                "workspace": str(workspace_dir),
                "options": {"refA": f"run:{a.manifest_id[:12]}"},
            }
        )


# -------------------------------------------------------------- _WS_CACHE


def test_ws_cache_bounded_and_closes_evicted_stores(tmp_path, monkeypatch):
    import ctx.mcp as mcp

    mcp._WS_CACHE.clear()
    workspaces = []
    for i in range(mcp._WS_CACHE_MAXSIZE + 3):
        d = tmp_path / f"proj{i}"
        d.mkdir()
        (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
        workspaces.append(str(d))

    stores = []
    for w in workspaces:
        monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
        _ws, store = mcp._resolve_cached(w)
        store.db  # force the sqlite connection open so eviction has something to close
        stores.append(store)

    assert len(mcp._WS_CACHE) <= mcp._WS_CACHE_MAXSIZE, "cache must stay bounded"

    evicted_count = len(workspaces) - mcp._WS_CACHE_MAXSIZE
    for w, store in zip(workspaces[:evicted_count], stores[:evicted_count]):
        assert w not in mcp._WS_CACHE
        assert store._db is None, "evicted Store must have its sqlite connection closed"

    for w in workspaces[evicted_count:]:
        assert w in mcp._WS_CACHE

    mcp._WS_CACHE.clear()


def test_ws_cache_ttl_expiry_closes_stale_store(tmp_path, monkeypatch):
    import ctx.mcp as mcp

    mcp._WS_CACHE.clear()
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")

    monkeypatch.setattr(mcp, "_WS_CACHE_TTL", 0.0)
    _ws1, store1 = mcp._resolve_cached(str(d))
    store1.db  # open the connection

    # TTL is 0: any subsequent resolve treats the entry as immediately stale.
    _ws2, store2 = mcp._resolve_cached(str(d))
    assert store1._db is None, "expired Store must be closed on eviction"
    assert store2 is not store1

    mcp._WS_CACHE.clear()


def test_ws_cache_hit_refreshes_lru_order(tmp_path, monkeypatch):
    """A cache hit must count as recent use — the *next* eviction should
    take the least-recently-*used* entry, not the least-recently-*inserted*
    one, otherwise a hot workspace gets evicted out from under a caller."""
    import ctx.mcp as mcp

    mcp._WS_CACHE.clear()
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    dirs = []
    for i in range(mcp._WS_CACHE_MAXSIZE):
        p = tmp_path / f"w{i}"
        p.mkdir()
        (p / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
        dirs.append(str(p))

    for w in dirs:
        mcp._resolve_cached(w)

    # Touch the oldest entry again so it becomes the most-recently-used.
    oldest = dirs[0]
    mcp._resolve_cached(oldest)

    # Now push one more distinct workspace in, forcing exactly one eviction.
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    mcp._resolve_cached(str(extra))

    assert oldest in mcp._WS_CACHE, "recently-touched entry must survive eviction"
    assert dirs[1] not in mcp._WS_CACHE, "the true least-recently-used entry should be evicted"

    mcp._WS_CACHE.clear()
