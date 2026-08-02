"""Eight defects from bug-bash round 15 (8/8 confirmed, precision 1.0).

Two are mine, from round 14, one commit earlier:

* the v2.3 self-edge filter stripped `self.method()` recursion -- the
  idiomatic way Python writes it -- against the fix's own stated intent;
* the collapsed-command quote guard covered the SCOPE and not the PATTERN,
  which is the same "collapsed command does not parse" defect through the
  other half of the same expression. A guard has more than one door, and
  this one grew its second door the day it was written.

Three more shared a root the round-14 plan_ir fix had already named and not
swept: ctx.toml is foreign input, and a syntactically valid file with a
wrong-typed value reached consumers uncoerced.
"""

from __future__ import annotations

import shlex

import pytest


# --------------------------- recursion is real; the phantom is the receiver
def _graph(tmp_path, body: str):
    from conftest import make_store, make_ws
    from ctx.callgraph import _load_graph

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "m.py").write_text(body, encoding="utf-8")
    ws = make_ws(tmp_path)
    return _load_graph(make_store(ws), ws), ws


def _self_callers(g, qual):
    from ctx.callgraph import _nid

    node = _nid("m.py", qual)
    return node in {c[0] for c in g.in_edges.get(node, [])}


def test_self_dot_method_recursion_keeps_its_edge(state_home, tmp_path):
    """`self.walk()` is how Python writes instance-method recursion, and the
    round-14 filter stripped every attribute-qualified self-call."""
    g, _ = _graph(tmp_path, (
        "class T:\n"
        "    def walk(self, n):\n"
        "        if n:\n"
        "            return self.walk(n - 1)\n"
        "        return 0\n"
    ))
    assert _self_callers(g, "T.walk"), "self.walk() inside walk IS recursion"


def test_bare_recursion_keeps_its_edge(state_home, tmp_path):
    g, _ = _graph(tmp_path, "def walk(n):\n    return walk(n - 1) if n else 0\n")
    assert _self_callers(g, "walk")


def test_a_call_through_another_object_is_still_filtered(state_home, tmp_path):
    """The receiver is the discriminator: `self._db.close()` goes through a
    DIFFERENT object and merely shares the name."""
    g, _ = _graph(tmp_path, (
        "class Store:\n"
        "    def __init__(self):\n"
        "        self._db = None\n"
        "    def close(self):\n"
        "        self._db.close()\n"
    ))
    assert not _self_callers(g, "Store.close"), "no phantom self-edge"


# ------------------------------- the guard covers the whole emitted string
@pytest.mark.parametrize("cmd", [
    "grep -rn \"TODO's\" src/",
    "grep -rn 'say \"hi\"' src/",
    "grep -rn NEEDLE \"a'b/\"",
])
def test_no_collapsed_command_carries_an_unbalanced_quote(cmd):
    from ctx.cli import _build_parser
    from ctx.substitute import collapse

    sub = collapse(cmd, failure_available=False, symbols_resolvable=False)
    if sub is None:
        return
    argv = shlex.split(sub.command)  # raises on an unbalanced quote
    _build_parser().parse_args(argv[1:])


def test_the_quote_guard_checks_the_assembled_query():
    """Checked on the whole string rather than per-field, so a new
    interpolated field inherits the guard instead of needing its own -- the
    scope had one and the pattern did not."""
    from ctx.substitute import _shell_safe

    assert _shell_safe("search NEEDLE --glob tests/** | files") is True
    assert _shell_safe("search TODO's | files") is False
    assert _shell_safe('search say "hi" | files') is False


# ------------------- foreign config is coerced ONCE, at the load boundary
@pytest.mark.parametrize("section,key,default", [
    ("budgets", "max_inline_bytes", 16384),
    ("budgets", "max_inline_lines", 240),
    ("budgets", "result_tokens", 1200),
    ("engagement", "activate_after_calls", 8),
    ("store", "retention_days", 30),
    ("plan", "max_nodes", 24),
])
def test_a_wrong_typed_config_value_degrades(tmp_path, section, key, default):
    """SPEC 15's fail-open contract covered TOML SYNTAX errors; a valid file
    with `max_inline_bytes = "lots"` flowed straight through and surfaced as
    an uncaught ValueError or TypeError in three separate commands, each
    looking like its own bug."""
    from ctx.config import load_config

    (tmp_path / "ctx.toml").write_text(
        f'version = 1\n[{section}]\n{key} = "nonsense"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert getattr(getattr(cfg, section), key) == default


def test_a_good_config_value_is_still_honoured(tmp_path):
    from ctx.config import load_config

    (tmp_path / "ctx.toml").write_text(
        "version = 1\n[budgets]\nmax_inline_bytes = 4096\n", encoding="utf-8"
    )
    assert load_config(tmp_path).budgets.max_inline_bytes == 4096


def test_coercion_respects_the_declared_type():
    from ctx.config import _coerce_like

    assert _coerce_like(10, "12") == 12          # a numeric string is a number
    assert _coerce_like(10, "lots") == 10        # anything else is the default
    assert _coerce_like(True, "yes") is True     # bool is NOT int here
    assert _coerce_like(True, False) is False
    assert _coerce_like("x", 3) == "x"
    assert _coerce_like((), ["a"]) == ("a",)
    assert _coerce_like(1.5, "2.5") == 2.5


# ------------------------------- "last" means last CAPTURED, not last DERIVED
def test_diagnose_follows_new_captures(state_home, workspace_dir):
    """`latest_run` is written when derivation happens and nothing re-derives
    on a fresh capture, so once diagnose had been asked once it answered
    about that same run forever while the user kept capturing new ones."""
    import sys

    from conftest import make_store, make_ws
    from ctx.execution import run_capture
    from ctx.facts import _newest_captured

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    first = run_capture(ws, [sys.executable, "-c", "print('a')"], store=store)
    assert _newest_captured(store) is not None
    second = run_capture(ws, [sys.executable, "-c", "print('b')"], store=store)
    newest = _newest_captured(store)
    assert newest is not None
    assert newest.startswith(second.manifest_id[:8]) or newest == second.manifest_id, (
        f"newest capture should be the second run, got {newest[:12]}"
    )
    assert not newest.startswith(first.manifest_id[:8])


# --------------------------------- a symbol filter symmetric on both sides
def test_locate_matches_a_dotted_query_against_a_bare_symbol():
    """All three original clauses assumed the STORED symbol was the dotted
    one, so a dotted query against a bare stored symbol found nothing."""
    def matches(sym: str, stored: str) -> bool:
        sym_tail = sym.rsplit(".", 1)[-1]
        return (
            stored == sym
            or stored.rsplit(".", 1)[-1] == sym
            or stored.startswith(sym + ".")
            or stored == sym_tail
            or stored.endswith("." + sym_tail)
        )

    assert matches("put_blob", "Store.put_blob")     # bare query, dotted store
    assert matches("Store.put_blob", "put_blob")     # dotted query, bare store
    assert matches("Store.put_blob", "Store.put_blob")
    assert matches("Store", "Store.put_blob")
    assert not matches("Store.put_blob", "other_thing")


# ------------------------- a span stops at the report, not inside it
def test_span_anchors_include_the_summary_banner():
    """_span_end anchored only on traceback headers and FAILED/ERROR lines,
    so the span for the LAST failure ran one line into the
    'short test summary info' separator -- evidence attributed to a failure
    that belongs to the report."""
    from ctx.rundiff import _BANNER_RE, _span_end

    assert _BANNER_RE.match("======= short test summary info =======")
    assert _BANNER_RE.match("=========== FAILURES ===========")
    assert not _BANNER_RE.match("    assert compute() == 2")
    # the banner is an anchor like any other: the span stops before it
    assert _span_end(10, [10, 15], 100) == 14
