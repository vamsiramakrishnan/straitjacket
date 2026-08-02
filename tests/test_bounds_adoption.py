"""Adoption invariant for ctx.bounds.

Four consecutive bug-bash rounds surfaced the same defect class at a site the
mechanism had not reached yet: a caller-supplied bound floored with
``max(1, n)``, so an explicit request for *nothing* silently became a request
for *something*. Fixing them one at a time was losing to the rate at which
new ones were found.

This test ends the class instead. Every ``max(1, ...)`` left in ``src/ctx``
must appear in the allowlist below WITH A REASON. A new one fails this test
until someone either routes it through ``ctx.bounds`` or justifies it here,
which is the only way an idiom this easy to type stops coming back.

The legitimate uses fall into three groups and none of them is a bound on an
emission:

* **1-indexed line arithmetic** -- ``max(1, line - context)``. Line 0 does
  not exist, so clamping up is correct; nothing is being widened.
* **Divide-by-zero guards** -- ``total / max(1, n)``. The 1 is a denominator,
  not a cap.
* **Display rounding** -- "~1 tok" reads better than "~0 tok" for a non-empty
  input, and no slice is taken from it.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ctx"
_MAX1 = re.compile(r"max\(\s*1\s*,")

#: file -> why every ``max(1, ...)`` in it is NOT a bound on an emission.
ALLOWED: dict[str, str] = {
    "textutil.py": "display rounding of a token estimate; no slice taken",
    "query.py": "1-indexed line arithmetic (max(1, line - context))",
    "_retrieval/get.py": "1-indexed line arithmetic in the body range",
    "digest/base.py": "1-indexed line arithmetic for a context window",
    "digest/text.py": "config-supplied head budget, not a caller argument",
    "digest/tableprof.py": "column-alignment heuristic, not a bound",
    "repomap.py": "display rounding of a size estimate",
    "orchestrator.py": "internal token split; denominator guard",
    "hook.py": "internal scaling and percentage display",
    "skeleton.py": "1-indexed line clamp",
    "commands/admin.py": "divide-by-zero denominator guards",
    "plan_ops.py": "1-indexed line arithmetic for context windows",
}


def _hits() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "bounds.py":
            continue  # the mechanism itself
        rel = str(path.relative_to(SRC))
        lines = [
            f"{rel}:{i}: {ln.strip()}"
            for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if _MAX1.search(ln) and "bounds.count" not in ln
        ]
        if lines:
            found[rel] = lines
    return found


def test_no_unreviewed_max_one_floors():
    """A caller-supplied bound must never be floored to 1.

    `top 0`, `--buckets 0`, `--depth 0`, `--tail 0`, `--cap 0` and a
    `limit` of 0 all meant "give me one anyway" until ctx.bounds reached
    them. If this fails on a file you just touched, the question is whether
    your `max(1, ...)` is bounding an EMISSION (route it through
    bounds.count) or doing line arithmetic (add it to ALLOWED with a reason).
    """
    unexpected = {rel: v for rel, v in _hits().items() if rel not in ALLOWED}
    assert not unexpected, (
        "new max(1, ...) outside the reviewed allowlist:\n  "
        + "\n  ".join(ln for v in unexpected.values() for ln in v)
    )


def test_allowlist_has_no_dead_entries():
    """An allowlist that outlives its sites rots into a lie."""
    hits = _hits()
    dead = sorted(set(ALLOWED) - set(hits))
    assert not dead, f"ALLOWED lists files with no max(1, ...) left: {dead}"


def test_caller_supplied_bounds_honour_zero():
    """The behaviour the sweep bought, asserted end to end."""
    from ctx import bounds

    for zero_means_zero in (0, -1, -10**9, float("nan")):
        assert bounds.count(zero_means_zero) == 0
    assert bounds.count(7) == 7


# --------------------------------------- zero must be safe, not just honoured
def test_zero_bounds_do_not_crash_the_swept_sites():
    """The sweep exposed a defensive floor doing load-bearing work.

    `max(1, n_buckets)` looked like it was only widening a request. It was
    also the reason `width = (hi - lo) / n_buckets` never divided by zero.
    Routing the bound through bounds.count made the zero real and turned a
    silent widening into a ZeroDivisionError -- so honouring a zero is only
    half the contract; surviving it is the other half.
    """
    from ctx.query import Stream, _stage_histogram

    rows = [{"v": str(i)} for i in range(10)]
    empty = _stage_histogram(None, Stream("records", rows), ["v", "--buckets", "0"])
    assert empty.rows == [], "zero buckets is an empty census"

    normal = _stage_histogram(None, Stream("records", rows), ["v", "--buckets", "3"])
    assert len(normal.rows) == 3, "a real bucket count still works"


def test_zero_is_safe_for_every_swept_bound_shape():
    """The other thirteen sites reduce to a slice or a min(); both are total
    at zero. Asserted rather than assumed -- that assumption is exactly what
    was wrong about histogram."""
    from ctx import bounds

    assert [1, 2, 3][: bounds.count(0)] == []      # cap / limit sites
    assert min(bounds.count(0), 8) == 0            # depth / tail sites
