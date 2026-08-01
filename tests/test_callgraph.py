"""Acceptance: the call graph (ctx callers/callees/impact/impls).

Scoped resolution, qual-keyed edges, call-site lines, per-file caching, and —
the invariant the rest exists to protect — ambiguity disclosed on every verb
(SPEC §8). The corpus below reproduces, minimally, the three defects measured
on straitjacket itself:

  * `callees render` unioned 22 unrelated `render` methods with no note;
  * `callers LogProfile.detect` returned callers of an unrelated module-level
    `detect`, labelled "exact by name", with no note;
  * `impact` disclosed nothing at all and walked an unqualified frontier.
"""

import subprocess

import pytest

SRC = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "def leaf():\n"
        "    return 1\n\n\n"
        "def mid():\n"
        "    return leaf() + leaf()\n\n\n"
        "def top():\n"
        "    return mid()\n\n\n"
        "class Widget:\n"
        "    def render(self):\n"
        "        return leaf()\n"
    ),
    "pkg/other.py": (
        "from pkg.core import top\n\n\n"
        "def entry():\n"
        "    return top()\n\n\n"
        "def render():\n"  # a second 'render' → ambiguity
        "    return 0\n"
    ),
    # The cross-module collision: `hosts.detect` is a module-level function
    # with nothing to do with `LogProfile.detect`, and hosts.py does not
    # import profiles.py. v1 reported detect_all as a caller of both.
    "pkg/hosts.py": (
        "def detect(spec):\n"
        "    return spec\n\n\n"
        "def detect_all(specs):\n"
        "    return [detect(s) for s in specs]\n"
    ),
    "pkg/profiles.py": (
        "class Base:\n"
        "    def detect(self):\n"
        "        return None\n\n\n"
        "class LogProfile(Base):\n"
        "    def detect(self):\n"
        "        return 'log'\n\n\n"
        "class JsonProfile(Base):\n"
        "    def detect(self):\n"
        "        return 'json'\n"
    ),
    "pkg/driver.py": (
        "from pkg.profiles import Base, LogProfile\n\n\n"
        "def dispatch(profile: Base):\n"
        "    return profile.detect()\n"
    ),
    # Calls `leaf` without importing it: neither local nor import tier can
    # bind it, so the edge lands in the repo tier and must be declared.
    "pkg/loose.py": (
        "def use():\n"
        "    return leaf()\n"
    ),
    "tests/test_smoke.py": (
        "from pkg.core import leaf\n\n\n"
        "def test_leaf():\n"
        "    assert leaf() == 1\n"
    ),
}


@pytest.fixture()
def ws_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel, content in SRC.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


# ------------------------------------------------------------------ basics
def test_callers_direct(ws_store):
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, "leaf")
    assert "callers: 3" in out  # mid (x1 line), Widget.render, tests/test_smoke
    assert "mid" in out and "Widget.render" in out
    assert "pkg/core.py:" in out


def test_callers_carry_the_call_site_line_not_the_def_range(ws_store):
    """v1 printed the caller's definition range, so seeing the actual call
    cost another read. `mid` calls leaf on line 6; `Widget.render` on line 15."""
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, "leaf")
    assert "pkg/core.py:6" in out
    assert "pkg/core.py:15" in out


def test_callees_in_repo_only(ws_store):
    from ctx.callgraph import cmd_callees

    ws, store = ws_store
    out = cmd_callees(store, ws, "mid")
    assert "leaf" in out
    assert "print" not in out  # builtins are not in-repo defs


def test_impact_transitive_blast_radius(ws_store):
    from ctx.callgraph import cmd_impact

    ws, store = ws_store
    out = cmd_impact(store, ws, "leaf", depth=6)
    for node in ("mid", "top", "entry", "Widget.render"):
        assert node in out, f"{node} missing from blast radius"
    assert "transitive callers" in out


def test_impact_depth_bound(ws_store):
    from ctx.callgraph import cmd_impact

    ws, store = ws_store
    shallow = cmd_impact(store, ws, "leaf", depth=1)
    assert "mid" in shallow
    assert "entry" not in shallow  # 3 hops away


def test_unknown_symbol_is_clean(ws_store):
    from ctx.callgraph import cmd_callers, cmd_impact, cmd_impls

    ws, store = ws_store
    for fn in (cmd_callers, cmd_impact, cmd_impls):
        assert "no definition matching" in fn(store, ws, "does_not_exist")


# ------------------------------------------- SPEC §8 disclosure regressions
def test_ambiguity_disclosed_on_callers(ws_store):
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, "render")
    assert "ambiguous" in out
    assert "2 definitions" in out


def test_ambiguity_disclosed_on_callees(ws_store):
    """REGRESSION: v1 unioned every same-named definition's callees and said
    nothing. `render` names Widget.render and pkg.other.render."""
    from ctx.callgraph import cmd_callees

    ws, store = ws_store
    out = cmd_callees(store, ws, "render")
    assert "ambiguous" in out, "callees must disclose that it merged definitions"
    assert "2 definitions" in out


def test_ambiguity_disclosed_on_impact(ws_store):
    """REGRESSION: v1's impact carried no ambiguity note on any path."""
    from ctx.callgraph import cmd_impact

    ws, store = ws_store
    out = cmd_impact(store, ws, "render")
    assert "ambiguous" in out
    assert "2 definitions" in out


def test_no_answer_is_ever_labelled_exact(ws_store):
    """v1 printed 'callers (exact by name)' over a name-matched answer."""
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    assert "exact" not in cmd_callers(store, ws, "detect")


# --------------------------------------------------- scoped resolution
def test_qualified_query_does_not_leak_unrelated_same_name_callers(ws_store):
    """THE defect: `callers LogProfile.detect` must not return `detect_all`,
    which calls the unrelated module-level `hosts.detect`. hosts.py neither
    defines nor imports profiles.py."""
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, "LogProfile.detect")
    assert "detect_all" not in out, "unscoped cross-module collision leaked in"
    assert "hosts.py" not in out


def test_local_tier_binds_the_same_file_definition(ws_store):
    """`detect_all` calls `detect` in its own file: that edge is local."""
    from ctx.callgraph import _load_graph

    ws, store = ws_store
    g = _load_graph(store, ws)
    callers = g.in_edges.get("detect", [])
    assert any(c[0] == "detect_all" and c[2] == "local" for c in callers)


def test_import_tier_binds_across_a_direct_import(ws_store):
    """driver.py imports profiles.py, so `profile.detect()` resolves into the
    profile classes rather than to hosts.detect."""
    from ctx.callgraph import _load_graph

    ws, store = ws_store
    g = _load_graph(store, ws)
    edges = [
        (target, c)
        for target, cs in g.in_edges.items()
        for c in cs
        if c[0] == "dispatch"
    ]
    assert edges, "dispatch must have a resolved callee"
    targets = {t for t, _ in edges}
    assert targets <= {"Base.detect", "LogProfile.detect", "JsonProfile.detect"}
    assert all(c[2] == "import" for _, c in edges)


def test_unscoped_edges_are_omitted_by_default_and_declared(ws_store):
    """`pkg/loose.py` calls `leaf` without importing it. That edge cannot be
    stated as fact, so it is held back — with its count and the flag that
    resolves it (CONTRIBUTING §4)."""
    from ctx.callgraph import _TIER_REPO, _load_graph, cmd_callers

    ws, store = ws_store
    g = _load_graph(store, ws)
    assert ("use", 2, _TIER_REPO) in g.in_edges["leaf"], "loose call must be repo-tier"

    default = cmd_callers(store, ws, "leaf")
    assert "use" not in default.split("omitted:")[0], "unscoped row leaked into the default"
    assert "omitted: 1 UNSCOPED" in default
    assert "--unscoped" in default, "omission needs a resolvable continuation"

    widened = cmd_callers(store, ws, "leaf", unscoped=True)
    assert "use" in widened
    assert "[unscoped]" in widened, "the widened row must stay labelled"


# --------------------------------------------------------------- impls
def test_impls_lists_subtypes(ws_store):
    from ctx.callgraph import cmd_impls

    ws, store = ws_store
    out = cmd_impls(store, ws, "Base")
    assert "LogProfile" in out and "JsonProfile" in out
    assert "subtypes: 2" in out
    assert "pkg/profiles.py:" in out


def test_impls_on_a_non_type_is_explicit(ws_store):
    from ctx.callgraph import cmd_impls

    ws, store = ws_store
    out = cmd_impls(store, ws, "leaf")
    assert "not a type" in out


def test_impls_reports_the_inverse_direction(ws_store):
    from ctx.callgraph import cmd_impls

    ws, store = ws_store
    assert "extends: Base" in cmd_impls(store, ws, "LogProfile")


# ------------------------------------------------- first-party vs test rows
def test_test_callers_are_grouped_not_interleaved(ws_store):
    from ctx.callgraph import cmd_callers

    ws, store = ws_store
    out = cmd_callers(store, ws, "leaf")
    lines = out.splitlines()
    assert any("tests/evals" in ln for ln in lines), "test callers need their own group"
    prod_idx = next(i for i, ln in enumerate(lines) if "pkg/core.py" in ln)
    test_idx = next(i for i, ln in enumerate(lines) if "tests/test_smoke.py" in ln)
    assert prod_idx < test_idx, "production callers come first"


# ------------------------------------------------------ caching / determinism
def test_determinism_and_cache(ws_store):
    from ctx.callgraph import cmd_impact

    ws, store = ws_store
    a = cmd_impact(store, ws, "leaf")
    b = cmd_impact(store, ws, "leaf")
    assert a == b
    units = store.root / "indexes" / "callgraph" / "units"
    graphs = store.root / "indexes" / "callgraph" / "graphs"
    assert units.is_dir() and any(units.iterdir())
    assert graphs.is_dir() and any(graphs.iterdir())


def test_editing_one_file_reuses_every_other_units_cache(ws_store):
    """REGRESSION: v1 keyed one blob on the whole corpus, so a single edit
    rebuilt everything. Unit keys are per file, so an edit to one file leaves
    every other file's cache entry addressable and reusable."""
    from ctx.callgraph import _load_graph, _unit_key

    ws, store = ws_store
    _load_graph(store, ws)
    before = {rel: _unit_key(ws, rel) for rel in SRC}

    p = ws.root / "pkg/other.py"
    p.write_text(
        p.read_text(encoding="utf-8") + "\n\ndef added():\n    return 1\n", encoding="utf-8"
    )

    after = {rel: _unit_key(ws, rel) for rel in SRC}
    changed = [rel for rel in SRC if before[rel] != after[rel]]
    assert changed == ["pkg/other.py"], f"only the edited file may re-key, got {changed}"

    g = _load_graph(store, ws)
    assert "added" in g.nodes, "the edited file must be re-parsed"


# ------------------------------------------------------------------ wiring
def test_cli_wiring(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    assert main(["--workspace", str(ws.root), "impact", "leaf"]) == 0
    assert "transitive callers" in capsys.readouterr().out


def test_cli_wiring_impls(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    assert main(["--workspace", str(ws.root), "impls", "Base"]) == 0
    assert "LogProfile" in capsys.readouterr().out
