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
