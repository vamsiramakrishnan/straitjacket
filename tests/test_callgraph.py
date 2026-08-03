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
    # Node ids are file-scoped ("rel::qual") since two files may define the
    # same name; the tier assertion is about the qual, so resolve through it.
    callers = [
        c for nid, cs in g.in_edges.items() if g.nodes[nid].qual == "detect" for c in cs
    ]
    assert any(g.nodes[c[0]].qual == "detect_all" and c[2] == "local" for c in callers)


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
        if g.nodes[c[0]].qual == "dispatch"
    ]
    assert edges, "dispatch must have a resolved callee"
    targets = {g.nodes[t].qual for t, _ in edges}
    assert targets <= {"Base.detect", "LogProfile.detect", "JsonProfile.detect"}
    assert all(c[2] == "import" for _, c in edges)


def test_unscoped_edges_are_omitted_by_default_and_declared(ws_store):
    """`pkg/loose.py` calls `leaf` without importing it. That edge cannot be
    stated as fact, so it is held back — with its count and the flag that
    resolves it (CONTRIBUTING §4)."""
    from ctx.callgraph import _TIER_REPO, _load_graph, cmd_callers

    ws, store = ws_store
    g = _load_graph(store, ws)
    leaf = next(n for n, d in g.nodes.items() if d.qual == "leaf")
    assert any(
        g.nodes[c[0]].qual == "use" and c[1] == 2 and c[2] == _TIER_REPO
        for c in g.in_edges[leaf]
    ), "loose call must be repo-tier"

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


# --------------------------------------------------------------- cycles
def test_cycles_finds_a_real_import_cycle(ws_store):
    """`ring_a` and `ring_b` import each other; nothing else in the corpus does."""
    from ctx.callgraph import cmd_cycles

    ws, store = ws_store
    (ws.root / "pkg/ring_a.py").write_text(
        "from pkg.ring_b import bee\n\n\ndef ay():\n    return bee()\n", encoding="utf-8"
    )
    (ws.root / "pkg/ring_b.py").write_text(
        "from pkg.ring_a import ay\n\n\ndef bee():\n    return 1\n", encoding="utf-8"
    )
    out = cmd_cycles(store, ws)
    assert "import cycles: 1" in out
    assert "repo:pkg/ring_a.py" in out and "repo:pkg/ring_b.py" in out


def test_cycles_reports_acyclic_plainly(ws_store):
    from ctx.callgraph import cmd_cycles

    ws, store = ws_store
    out = cmd_cycles(store, ws)
    assert "import cycles: 0" in out
    assert "acyclic" in out


def test_call_cycles_find_mutual_recursion(ws_store):
    from ctx.callgraph import cmd_cycles

    ws, store = ws_store
    (ws.root / "pkg/recur.py").write_text(
        "def ping(n):\n    return pong(n - 1)\n\n\ndef pong(n):\n    return ping(n - 1)\n",
        encoding="utf-8",
    )
    out = cmd_cycles(store, ws, calls=True)
    assert "call cycles: 1" in out
    assert "ping" in out and "pong" in out


def test_call_cycles_exclude_unscoped_edges_by_default(ws_store):
    """An unscoped edge does not merely add a row to a cycle search — it fuses
    unrelated components into one phantom cycle. `pkg/loose.py` calls `leaf`
    without importing it, so that edge must not create a cycle."""
    from ctx.callgraph import _TIER_REPO, _load_graph, cmd_cycles

    ws, store = ws_store
    # Make the loose (unscoped) call mutual, so including it WOULD form a cycle.
    (ws.root / "pkg/core.py").write_text(
        (ws.root / "pkg/core.py").read_text(encoding="utf-8") + "\n\ndef back():\n    return use()\n",
        encoding="utf-8",
    )
    g = _load_graph(store, ws)
    assert any(t == _TIER_REPO for cs in g.in_edges.values() for _, _, t in cs)

    assert "call cycles: 0" in cmd_cycles(store, ws, calls=True)


def test_cycles_are_deterministic(ws_store):
    from ctx.callgraph import cmd_cycles

    ws, store = ws_store
    assert cmd_cycles(store, ws) == cmd_cycles(store, ws)
    assert cmd_cycles(store, ws, calls=True) == cmd_cycles(store, ws, calls=True)


def test_scc_engine_agrees_with_and_without_networkx(monkeypatch):
    """The stdlib iterative Tarjan must return exactly what networkx does —
    the engine is a speed choice, not a semantic one."""
    import builtins

    from ctx.callgraph import _sccs

    adj = {
        "a": ["b"], "b": ["c"], "c": ["a"],       # 3-cycle
        "d": ["e"], "e": ["d"],                    # 2-cycle
        "f": ["g"], "g": [],                       # acyclic
        "h": ["h"],                                # self-loop
    }
    with_nx = _sccs(adj)

    real_import = builtins.__import__

    def no_networkx(name, *a, **kw):
        if name == "networkx":
            raise ImportError("blocked for this test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_networkx)
    without_nx = _sccs(adj)

    assert with_nx == without_nx
    assert with_nx == [["a", "b", "c"], ["d", "e"], ["h"]]


def test_scc_iterative_survives_a_deep_chain(monkeypatch):
    """Recursion depth in Tarjan is the longest path, so the recursive form
    would blow the stack on a long import chain. 5,000 nodes is under the
    module's _MAX_FILES bound and well over Python's recursion limit."""
    import builtins

    from ctx.callgraph import _sccs

    n = 5000
    adj = {f"n{i}": [f"n{i + 1}"] for i in range(n - 1)}
    adj[f"n{n - 1}"] = ["n0"]  # close it into one big cycle

    real_import = builtins.__import__

    def no_networkx(name, *a, **kw):
        if name == "networkx":
            raise ImportError("blocked for this test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_networkx)
    comps = _sccs(adj)
    assert len(comps) == 1 and len(comps[0]) == n


def test_cli_wiring_cycles(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    assert main(["--workspace", str(ws.root), "cycles"]) == 0
    assert "import cycles" in capsys.readouterr().out


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
    assert any(d.qual == "added" for d in g.nodes.values()), (
        "the edited file must be re-parsed"
    )


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


# ================================================ file-scoped node identity
def _tiny_ws(tmp_path, files: dict):
    """A workspace with exactly the files given, plus a ctx.toml."""
    from conftest import make_store, make_ws

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    ws = make_ws(tmp_path)
    return ws, make_store(ws)


def test_same_name_in_two_files_is_two_nodes(state_home, tmp_path):
    """Nodes were keyed by bare qual, so `nodes.setdefault` kept the first
    definition of a colliding name and DROPPED the rest. The second file's
    function was then absent from the graph entirely, and an unambiguous
    same-file call to it resolved to nothing."""
    from ctx.callgraph import _load_graph, cmd_callers

    ws, store = _tiny_ws(tmp_path, {
        "one.py": "def shared():\n    return 1\n",
        "two.py": "def shared():\n    return 2\n\n\ndef caller():\n    return shared()\n",
    })
    g = _load_graph(store, ws)
    shared = [n for n, d in g.nodes.items() if d.qual == "shared"]
    assert len(shared) == 2, f"both definitions must exist as nodes: {shared}"
    assert {g.nodes[n].rel for n in shared} == {"one.py", "two.py"}

    out = cmd_callers(store, ws, "shared")
    assert "caller" in out, "the same-file caller must be reported, not hidden"
    assert "2 definitions match 'shared' (ambiguous)" in out, (
        "an ambiguous name is answered out loud, never merged silently"
    )


def test_unrelated_hierarchies_sharing_a_name_are_not_merged(state_home, tmp_path):
    """The false-positive half of the same root: subtype edges recorded
    against a bare-name `Base` node were reported for whichever `Base` the
    query matched, asserting that SubTwo extends one.py's class."""
    from ctx.callgraph import _load_graph, cmd_impls

    ws, store = _tiny_ws(tmp_path, {
        "one.py": "class Base:\n    pass\n\n\nclass SubOne(Base):\n    pass\n",
        "two.py": "class Base:\n    pass\n\n\nclass SubTwo(Base):\n    pass\n",
    })
    g = _load_graph(store, ws)
    bases = sorted(n for n, d in g.nodes.items() if d.qual == "Base")
    assert len(bases) == 2
    subs = {b: [g.nodes[s].qual for s in g.subclasses.get(b, [])] for b in bases}
    assert subs[bases[0]] == ["SubOne"], subs
    assert subs[bases[1]] == ["SubTwo"], subs

    out = cmd_impls(store, ws, "Base")
    assert "2 definitions match 'Base' (ambiguous)" in out


def test_node_ids_never_leak_into_rendered_output(state_home, tmp_path):
    """A node id is internal. Every renderer prints the qual and the file
    separately -- the cache round-trip once rebuilt _Def from the KEY and
    started printing `pkg/core.py::Widget.render` as a symbol name."""
    from ctx.callgraph import cmd_callers, cmd_impact, cmd_impls

    ws, store = _tiny_ws(tmp_path, {
        "one.py": "class Base:\n    pass\n\n\ndef shared():\n    return 1\n",
        "two.py": "from one import shared\n\n\ndef caller():\n    return shared()\n",
    })
    for out in (cmd_callers(store, ws, "shared"),
                cmd_impact(store, ws, "shared"),
                cmd_impls(store, ws, "Base")):
        assert "py::" not in out, f"node id leaked into output:\n{out}"


def test_graph_cache_round_trips_the_qual(state_home, tmp_path):
    """The cached path must answer identically to the cold one. It did not:
    _graph_from_json rebuilt _Def(key, ...) back when the key WAS the qual,
    so file-scoped ids silently corrupted every cached qual."""
    from ctx.callgraph import cmd_impact

    ws, store = _tiny_ws(tmp_path, {
        "one.py": "def leaf():\n    return 1\n\n\ndef mid():\n    return leaf()\n",
    })
    cold = cmd_impact(store, ws, "leaf")
    warm = cmd_impact(store, ws, "leaf")
    assert cold == warm, "cached and uncached answers must be byte-identical"


# ------------------------------------------- nested defs own their own calls
def test_nested_function_calls_belong_to_the_nested_function_only(
    state_home, tmp_path
):
    """`_function` walked with ast.walk and `continue`d past nested defs.
    ast.walk enqueues a node's children BEFORE yielding the node, so the
    guard skipped only the FunctionDef itself -- which is never a Call, so it
    was a no-op that read as if it worked. Every call inside a closure was
    attributed to the enclosing function too."""
    from ctx.callgraph import _load_graph

    ws, store = _tiny_ws(tmp_path, {
        "m.py": (
            "def target():\n    return 1\n\n\n"
            "def outer():\n"
            "    def inner():\n"
            "        return target()\n"
            "    return inner\n"
        ),
    })
    g = _load_graph(store, ws)
    callers = {
        g.nodes[c[0]].qual
        for nid, cs in g.in_edges.items() if g.nodes[nid].qual == "target"
        for c in cs
    }
    assert callers == {"outer.inner"}, f"only the nested def calls target: {callers}"


def test_class_bodies_and_lambdas_stay_with_the_enclosing_scope(
    state_home, tmp_path
):
    """The pruning boundary is 'things that get their own node' -- nested
    defs. A lambda body gets no node, so pruning it would DROP its call
    sites; a class body genuinely executes in the enclosing scope."""
    from ctx.callgraph import _load_graph

    ws, store = _tiny_ws(tmp_path, {
        "m.py": (
            "def target():\n    return 1\n\n\n"
            "def outer():\n"
            "    f = lambda: target()\n"
            "    class C:\n"
            "        v = target()\n"
            "    return f, C\n"
        ),
    })
    g = _load_graph(store, ws)
    callers = {
        g.nodes[c[0]].qual
        for nid, cs in g.in_edges.items() if g.nodes[nid].qual == "target"
        for c in cs
    }
    assert "outer" in callers, f"lambda/class-body calls must not vanish: {callers}"


def test_decorator_on_a_nested_def_evaluates_in_the_enclosing_scope(
    state_home, tmp_path
):
    """A decorator EXPRESSION runs where the def is written, not inside it.
    (`@deco` bare is an ast.Name and no call site at all -- only `@deco(...)`
    is one, which is why the factory form is what this asserts.)"""
    from ctx.callgraph import _load_graph

    ws, store = _tiny_ws(tmp_path, {
        "m.py": (
            "def deco(n):\n    return lambda fn: fn\n\n\n"
            "def outer():\n"
            "    @deco(1)\n"
            "    def inner():\n"
            "        return 2\n"
            "    return inner\n"
        ),
    })
    g = _load_graph(store, ws)
    callers = {
        g.nodes[c[0]].qual
        for nid, cs in g.in_edges.items() if g.nodes[nid].qual == "deco"
        for c in cs
    }
    assert callers == {"outer"}, callers
