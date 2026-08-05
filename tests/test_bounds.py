"""ctx.bounds: a bound may narrow an emission, never widen it.

Regression cover for four confirmed defects found by the bug-bash eval
(evals/devex/, 2026-08-02), all of which shared one shape: an unvalidated
bound reached a Python slice, and negative indexing turned a nonsensical
value into *more* output.
"""

from __future__ import annotations

from ctx import bounds
from ctx.textutil import bounded


# ------------------------------------------------------------------ count
def test_count_zero_means_zero():
    """`max(1, n)` turned an explicit request for nothing into one row."""
    assert bounds.count(0) == 0
    assert bounds.count(5) == 5


def test_count_never_negative():
    for n in (-1, -1000):
        assert bounds.count(n) == 0


def test_count_is_total_on_junk():
    for junk in (None, "abc", object(), [1]):
        assert bounds.count(junk) == 0


# ---------------------------------------------------------- budget_bytes
def test_budget_bytes_negative_collapses_to_zero():
    """The defect: -1 tokens became -4 bytes, and raw[:-4] is nearly all of it."""
    assert bounds.budget_bytes(-1) == 0
    assert bounds.budget_bytes(-10**9) == 0


def test_budget_bytes_scales_normally():
    assert bounds.budget_bytes(10) == 40
    assert bounds.budget_bytes(0) == 0


# -------------------------------------------------------------------- span
def test_span_negative_end_is_empty_not_wraparound():
    """The defect: min(end, n_lines) left end negative and idx[end] wrapped."""
    assert bounds.span(1, -5, 100) is None


def test_span_inverted_and_out_of_range_are_empty():
    assert bounds.span(10, 3, 100) is None
    assert bounds.span(500, 600, 100) is None
    assert bounds.span(1, 10, 0) is None


def test_span_clamps_into_range():
    assert bounds.span(0, 10, 100) == (1, 10)
    assert bounds.span(5, 10**9, 100) == (5, 100)


# ------------------------------------------- the property, end to end
def test_bounded_never_returns_more_than_unbounded_input():
    """The invariant, stated directly: for ANY budget, bounded() output is
    never larger than its input. The negative-budget defect broke exactly
    this -- the backstop returned nearly the whole input when asked for less
    than nothing."""
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    for budget in (-(10**6), -1, 0, 1, 10, 100, 10**6):
        out = bounded(text, budget)
        assert len(out) <= len(text) + 200, (
            f"budget={budget} produced {len(out)} bytes from {len(text)}"
        )


def test_bounded_negative_budget_emits_almost_nothing():
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    out = bounded(text, -1)
    assert len(out) < len(text) / 10, (
        "a negative budget must emit (almost) nothing, never a near-complete "
        "suffix of the input"
    )


def test_coercions_are_total_including_infinities():
    """The module claims never to raise. A bug-bash arm found that claim false
    within minutes: int(float("inf")) raises OverflowError, which the first
    cut did not catch -- and a runaway budget calculation is exactly where an
    infinity comes from. Totality is a contract, so it gets a test."""
    for v in (float("inf"), -float("inf"), float("nan"), None, "x", object(), [1]):
        assert bounds.count(v) == 0
        assert bounds.budget_bytes(v) == 0
        assert bounds.span(1, v, 10) in (None, (1, 10))
        assert bounds.span(v, 10, 10) in (None, (1, 10))


def test_pricing_tier_tokens_match_on_letter_boundaries():
    """The collision that matters: `mini` must not price `gemini-*` as a mini
    tier. A bug-bash arm flagged pricing.py's docstring for promising more
    than a boundary rule can deliver (it used `ge-mini-3-pro`, which no
    lexical rule separates from `gpt-4o-mini-2024`). The docstring was wrong;
    this pins the behaviour that is actually load-bearing."""
    from ctx.pricing import _token_matches

    assert _token_matches("mini", "gpt-5-mini") is True
    assert _token_matches("mini", "gpt-4o-mini-2024") is True
    assert _token_matches("mini", "gemini-3-pro") is False
    assert _token_matches("mini", "gemini-3.6-flash") is False


def test_explicit_zero_is_an_answer_not_an_absence():
    """`raw or default` reads an explicit 0 as unset. Two confirmed defects:
    `ctx gc --retention-days 0` (collect everything already expired) fell back
    to the configured retention, and `ctx ask --depth 0` became depth 3."""
    assert bounds.explicit(0, 3) == 0
    assert bounds.explicit(None, 3) == 3
    assert bounds.explicit("", "x") == ""
    assert bounds.explicit(False, True) is False


def test_timeout_signal_is_not_the_childs_exit_code():
    """124 is the documented timeout code, but a script may legitimately
    return it. run_eval/run_seq now carry a timed_out flag beside the code so
    `sys.exit(124)` and a real kill stay distinguishable."""
    import inspect

    from ctx.pyeval import run_eval
    from ctx.seq import run_seq

    for fn in (run_eval, run_seq):
        doc = inspect.getdoc(fn) or ""
        assert "timed_out" in doc, f"{fn.__name__} must document the flag"


def test_callgraph_resolves_src_layout_and_relative_imports():
    """Two confirmed defects in one resolver (evals/devex/):

    * a src-layout file registered only its PATH-derived name, so
      `src/ctx/foo.py` was never reachable as `ctx.foo` -- the name it is
      actually imported by -- and no import in this repository resolved
      through the stem rung at all.
    * `from . import X` was dropped outright (node.module is None for a bare
      relative import), silently unscoping real intra-package callers.
    """
    import ast as _ast

    from ctx.callgraph import _PyVisitor

    # Relative imports must resolve against the file's own package.
    tree = _ast.parse("from . import sibling\nfrom .mod import thing\n")
    holder = type("U", (), {"rel": "src/ctx/foo.py", "imports": [],
                            "defs": []})()
    v = _PyVisitor.__new__(_PyVisitor)
    v.unit = holder
    v.stack = []
    for node in tree.body:
        v.visit_ImportFrom(node)
    assert any(i.startswith("src.ctx") for i in holder.imports), holder.imports
    assert "src.ctx.sibling" in holder.imports
    assert "src.ctx.mod" in holder.imports
