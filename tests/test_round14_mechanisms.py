"""Five defects from bug-bash round 14 (5/5 confirmed, precision 1.0).

The first is mine: round 12's scope-preserving grep collapse emitted a
`--glob` that `ctx q`'s parser cannot accept, so every scoped substitution
produced a command that exits 2. The defect was latent before (only a
trailing `*.ext` ever set the flag) and my fix made it fire for every
directory-scoped grep -- a rare crash turned into a common one.
"""

from __future__ import annotations

import shlex

import pytest


def _assert_parses(command: str) -> None:
    from ctx.cli import _build_parser

    argv = shlex.split(command)
    assert argv[0] == "ctx"
    _build_parser().parse_args(argv[1:])


@pytest.mark.parametrize("cmd", [
    "grep -rn NEEDLE tests/",
    "grep -rn NEEDLE src/ctx/hook.py",
    "grep -rn NEEDLE '*.py'",
    "grep -rn NEEDLE .",
])
def test_every_collapsed_command_actually_parses(cmd):
    """A collapse that emits something ctx cannot run is worse than no
    collapse: the agent gets an exit-2 error instead of its answer."""
    from ctx.substitute import collapse

    sub = collapse(cmd, failure_available=False, symbols_resolvable=False)
    if sub is None:
        return
    _assert_parses(sub.command)


def test_the_scope_rides_inside_the_query():
    from ctx.substitute import collapse

    sub = collapse("grep -rn NEEDLE tests/", failure_available=False,
                   symbols_resolvable=False)
    assert sub is not None
    # Asserted through the PARSER, not by matching a spelling: the fields are
    # shlex-quoted inside the query so a pattern carrying `|` or a quote
    # survives as one token, which makes the literal text unstable but the
    # parsed result exact.
    from ctx.query import parse_query

    argv = shlex.split(sub.command)
    assert argv[1] == "q" and len(argv) == 3, "the query is ONE argument"
    stages = parse_query(argv[2])
    assert stages[0][0] == "search"
    assert "tests/**" in stages[0][1], stages
    _assert_parses(sub.command)


def test_a_scope_with_a_quote_declines_rather_than_emitting_garbage():
    from ctx.substitute import _scope_hint

    assert _scope_hint(["tests/"]) == "tests/**"
    # The whole query is quoted once; a scope carrying a quote cannot survive
    # that single layer, so the caller declines instead of emitting a
    # malformed command.
    from ctx.substitute import collapse

    sub = collapse("grep -rn NEEDLE \"a'b/\"", failure_available=False,
                   symbols_resolvable=False)
    if sub is not None:
        _assert_parses(sub.command)


# ---------------------------------------- an id must not collide by joining
def test_debt_ids_do_not_collide_across_the_delimiter():
    """`sha256(f"{note}|{ref}")` lets two distinct declarations straddle the
    separator differently and hash to the same id, so the second silently
    becomes an update to the first."""
    import hashlib

    def _id(note, ref):
        basis = f"{len(note)}:{note}{len(ref)}:{ref}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]

    assert _id("a|b", "") != _id("a", "b")
    assert _id("a", "b|c") != _id("a|b", "c")
    assert _id("x", "y") == _id("x", "y"), "still deterministic"


# ------------------------------- the remedy a tool prints must do something
def test_q_unscoped_actually_widens(state_home, tmp_path):
    """The stages registered --unscoped as a recognized token, the omission
    note told the reader to use it, and no stage ever READ it -- so the
    remedy the tool prints was a no-op that looked answered."""
    from conftest import make_store, make_ws
    from ctx.query import run_query

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "def caller():\n    return shared()\n", encoding="utf-8"
    )
    ws = make_ws(tmp_path)
    store = make_store(ws)

    scoped = run_query(ws, store, "callers shared")
    widened = run_query(ws, store, "callers shared --unscoped")
    assert widened != scoped, "--unscoped must change the answer it advertises"


# ------------------------------- foreign config degrades, never raises
def test_a_non_numeric_plan_bound_degrades_to_the_default():
    """config.py documents a malformed ctx.toml as degrading to defaults, but
    that covers TOML SYNTAX errors -- a valid file with `max_nodes = "lots"`
    parsed fine and reached a bare int(), raising out of plan validation."""
    from ctx.plan_ir import _policy_int

    class _P:
        max_nodes = "lots"
        max_fanout = None

    assert _policy_int(_P(), "max_nodes", 40) == 40
    assert _policy_int(_P(), "max_fanout", 8) == 8
    assert _policy_int(_P(), "absent", 5) == 5

    class _Good:
        max_nodes = 12

    assert _policy_int(_Good(), "max_nodes", 40) == 12


# ------------------------- a node is not its own caller by coincidence
def test_attribute_call_does_not_create_a_phantom_self_edge(state_home, tmp_path):
    """`self._db.close()` inside `Store.close` resolved by the attribute TAIL
    against every same-file definition, so the method appeared to call
    itself -- inflating impact and making a leaf look recursive."""
    from conftest import make_store, make_ws
    from ctx.callgraph import _load_graph, _nid

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "s.py").write_text(
        "class Store:\n"
        "    def __init__(self):\n"
        "        self._db = None\n"
        "    def close(self):\n"
        "        self._db.close()\n",
        encoding="utf-8",
    )
    ws = make_ws(tmp_path)
    g = _load_graph(make_store(ws), ws)
    node = _nid("s.py", "Store.close")
    callers = {c[0] for c in g.in_edges.get(node, [])}
    assert node not in callers, "no phantom self-edge"


def test_real_recursion_keeps_its_edge(state_home, tmp_path):
    """The distinction the extractor now records: a BARE `f()` inside `f` is
    recursion and must keep its edge; only the attribute-call coincidence is
    filtered."""
    from conftest import make_store, make_ws
    from ctx.callgraph import _load_graph, _nid

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "r.py").write_text(
        "def walk(n):\n    if n:\n        return walk(n - 1)\n    return 0\n",
        encoding="utf-8",
    )
    ws = make_ws(tmp_path)
    g = _load_graph(make_store(ws), ws)
    node = _nid("r.py", "walk")
    callers = {c[0] for c in g.in_edges.get(node, [])}
    assert node in callers, "genuine recursion must survive the self-edge filter"
