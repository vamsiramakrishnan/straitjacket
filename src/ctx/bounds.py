"""Bound coercion whose failure direction is always *less* output.

Every emission in this project is supposed to be bounded, and every bound
arrives as a caller-supplied integer: a token budget, a row count, a line
span. Python's slicing is permissive about those integers in exactly the
wrong direction -- ``raw[:-4]`` is not "four bytes" but "everything except
the last four", and ``idx[-5]`` is not an error but a seek near the end.

A bug bash over this repository (evals/devex/, 2026-08-02) confirmed three
separate defects that all reduce to that one shape -- an unvalidated bound
reaching a slice, where a nonsensical value silently *widened* the output:

* ``textutil.bounded(text, budget_tokens=-1)`` computed ``budget_bytes=-4``
  and returned ``raw[:-4]`` -- almost the entire input, from the function
  documented as the hard backstop that output "must NEVER exceed".
* ``Store.read_blob_lines(h, start, end=-5)`` clamped with
  ``min(end, n_lines)``, left ``end`` negative, and ``idx[end]`` wrapped
  around to dump most of the blob.
* ``query._stage_top("0")`` did ``max(1, int(raw))`` and returned one row
  for ``top 0`` -- a small widening, but the same inversion.

For a tool whose entire claim is that bytes cannot escape unbounded, a
bound that widens output on bad input is the worst available failure mode.
This module makes the safe direction structural: **a bound may narrow an
emission, never widen it, and a nonsensical bound yields the empty result
rather than the whole input.**

These helpers are deliberately total -- they never raise. A caller that
wants to reject bad input should validate before calling; a caller on an
emission path wants the floor, not an exception, because an exception on
the way out of a digest is itself an unbounded failure.

That totality has to be enforced, not merely asserted: the first cut of this
module guarded ``(TypeError, ValueError)`` and a bug-bash arm immediately
found that ``int(float("inf"))`` raises ``OverflowError``, so the "never
raises" claim was false for the one input a runaway budget calculation is
most likely to produce. Every coercion below catches OverflowError too.
"""

from __future__ import annotations

__all__ = ["count", "budget_bytes", "span", "explicit"]


def count(raw: object, *, default: int = 0) -> int:
    """A non-negative item count. Zero means zero.

    ``max(1, n)`` is the tempting spelling and it is wrong: it turns an
    explicit request for nothing into a request for something. Anything that
    is not a non-negative integer collapses to ``default`` (0), so the
    failure direction stays toward less output.
    """
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return max(0, default)
    return n if n > 0 else 0


def budget_bytes(budget_tokens: object, *, bytes_per_token: int = 4) -> int:
    """Byte budget for a token budget, never negative.

    A negative or unparseable token budget yields 0 -- emit nothing -- which
    a slice reads as an empty prefix instead of a near-complete suffix.
    """
    try:
        tokens = int(budget_tokens)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, tokens) * max(0, int(bytes_per_token))


def span(start: object, end: object, total: int) -> tuple[int, int] | None:
    """Clamp a 1-indexed inclusive ``[start, end]`` line span into ``[1, total]``.

    Returns ``None`` for an empty span -- out of range, inverted, or
    nonsensical -- so callers branch explicitly instead of handing a
    negative index to a slice and getting a wraparound.
    """
    if total <= 0:
        return None
    try:
        lo = int(start)  # type: ignore[arg-type]
        hi = int(end)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    lo = max(1, lo)
    if hi < 0 or lo > total or hi < lo:
        return None
    return lo, min(hi, total)


def explicit(raw: object, default: object) -> object:
    """A caller-supplied value, honouring an explicit zero.

    ``raw or default`` is the idiom that breaks this, and it is everywhere:
    ``0`` is falsy, so an explicit "none of it" is indistinguishable from
    "unset" and silently becomes the default. A bug bash confirmed two
    instances on the same day the sibling defect (``max(1, n)`` flooring a
    ``top 0`` to one row) was fixed:

    * ``ctx gc --retention-days 0`` -- collect everything already expired --
      fell back to the configured retention instead, so the one spelling that
      means "now" was the one spelling that did nothing.
    * ``ctx ask --depth 0`` became depth 3.

    Only ``None`` means unset. Zero, empty string and empty list are answers.
    """
    return default if raw is None else raw
