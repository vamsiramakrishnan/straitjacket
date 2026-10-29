"""Adoption invariant for ctx.bounds.

Bug-bash rounds kept surfacing the same defect class at a site the mechanism
had not reached yet: a caller-supplied bound that quietly means MORE output
instead of less. Fixing them one at a time was losing to the rate at which
new ones were found, so this test ends the class instead -- a new occurrence
fails until someone either routes it through ``ctx.bounds`` or justifies it
in the allowlist below WITH A REASON.

The class has THREE spellings, and the first version of this test knew only
the first. Round 7 then found two more defects by walking straight through
the other two:

1. ``max(1, n)``   -- an explicit request for nothing becomes one row.
2. ``n or DEFAULT`` -- an explicit 0 reads as "unset" and the default wins
   (``ctx search --max-matches 0`` silently became 80).
3. ``xs[-n:]``     -- at n == 0 this is ``xs[0:]``, the WHOLE list, so
   ``ctx job <id> --tail 0`` dumped the entire spool. A negative n slices
   from the front and widens too.

Detection is AST-based, not textual. The text version matched ``max(1, n)``
inside a docstring EXPLAINING the defect, which is both a false positive and
a good demonstration that a grep does not know what code is.

Legitimate ``max(1, ...)`` uses fall into three groups and none of them is a
bound on an emission:

* **1-indexed line arithmetic** -- ``max(1, line - context)``. Line 0 does
  not exist, so clamping up is correct; nothing is being widened.
* **Divide-by-zero guards** -- ``total / max(1, n)``. The 1 is a
  denominator, not a cap.
* **Display rounding** -- "~1 tok" reads better than "~0 tok" for a
  non-empty input, and no slice is taken from it.
"""

from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ctx"

#: Identifiers that name a BOUND. `n or 80` on one of these is the
#: zero-means-unset defect; on `name or "anon"` it is an ordinary default.
_BOUNDISH = re.compile(
    r"(?:^|_)(?:cap|caps|limit|limits|max|maximum|max_[a-z_]+|[a-z_]*_max|"
    r"depth|tail|top|budget|buckets|window|retention|retention_days|timeout)$"
)

#: A default that is an empty container or string is a CONTAINER default, not
#: a bound: `rows or {}` and `head or ""` mean "give me something iterable",
#: where 0 is not a value the caller could have meant. Only a numeric-shaped
#: default makes `or` the zero-means-unset defect.
_CONTAINER_DEFAULT = (ast.Dict, ast.List, ast.Tuple, ast.Set)

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
    "snapcompact.py": (
        "pixel-geometry floors, not caller bounds: a font's own metrics "
        "clamped to a positive cell size, and post-downscale dimensions "
        "clamped so a degenerate image never rounds to a zero-token size"
    ),
}

#: file -> why a ``x or DEFAULT`` on a bound-ish name is correct there.
ALLOWED_OR: dict[str, str] = {}

#: file -> why a ``xs[-n:]`` with a computed n is correct there.
#: Empty on purpose. Module constants and `len()`-based suffix tests are
#: excluded by the detector itself rather than listed here -- an allowlist
#: that names a whole FILE grants amnesty to every future slice in it, which
#: is exactly the leak an adoption invariant is supposed to close.
ALLOWED_NEG_SLICE: dict[str, str] = {}


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "bounds.py":
            continue  # the mechanism itself
        rel = str(path.relative_to(SRC))
        text = path.read_text(encoding="utf-8")
        try:
            yield rel, text, ast.parse(text)
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue


def _site(rel: str, node: ast.AST, text: str) -> str:
    line = getattr(node, "lineno", 0)
    src = text.splitlines()[line - 1].strip() if line else ""
    return f"{rel}:{line}: {src}"


def _max_one_hits() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for rel, text, tree in _modules():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "max" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == 1:
                found.setdefault(rel, []).append(_site(rel, node, text))
    return found


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _zero_means_unset_hits() -> dict[str, list[str]]:
    """``bound or DEFAULT`` -- an explicit 0 read as an absence."""
    found: dict[str, list[str]] = {}
    for rel, text, tree in _modules():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
                continue
            name = _name_of(node.values[0])
            if not (name and _BOUNDISH.search(name)):
                continue
            default = node.values[-1]
            if isinstance(default, _CONTAINER_DEFAULT):
                continue
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                continue
            found.setdefault(rel, []).append(_site(rel, node, text))
    return found


def _negative_slice_hits() -> dict[str, list[str]]:
    """``xs[-n:]`` with a computed n -- everything at 0, the front at < 0."""
    found: dict[str, list[str]] = {}
    for rel, text, tree in _modules():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
                continue
            lower = node.slice.lower
            if not (isinstance(lower, ast.UnaryOp) and isinstance(lower.op, ast.USub)):
                continue
            operand = lower.operand
            if isinstance(operand, ast.Constant):
                continue  # a literal window is fixed, not caller-supplied
            if isinstance(operand, ast.Name) and operand.id.isupper():
                continue  # a module constant is not a caller-supplied bound
            if (
                isinstance(operand, ast.Call)
                and isinstance(operand.func, ast.Name)
                and operand.func.id == "len"
            ):
                # `x[-len(want):] == want` is a SUFFIX TEST, not a window: it
                # takes exactly as much as it compares against, and at zero
                # compares against the empty string, which is still correct.
                continue
            found.setdefault(rel, []).append(_site(rel, node, text))
    return found


def _assert_reviewed(hits, allowed, what: str, remedy: str):
    unexpected = {rel: v for rel, v in hits.items() if rel not in allowed}
    assert not unexpected, (
        f"unreviewed {what}:\n  "
        + "\n  ".join(ln for v in unexpected.values() for ln in v)
        + f"\n{remedy}"
    )
    dead = sorted(set(allowed) - set(hits))
    assert not dead, f"ALLOWED* lists files with no {what} left: {dead}"


# ------------------------------------------------- spelling 1: max(1, n)
def test_no_unreviewed_max_one_floors():
    """A caller-supplied bound must never be floored to 1.

    `top 0`, `--buckets 0`, `--depth 0`, `--tail 0`, `--cap 0` and a `limit`
    of 0 all meant "give me one anyway" until ctx.bounds reached them.
    """
    _assert_reviewed(
        _max_one_hits(), ALLOWED, "max(1, ...)",
        "Is it bounding an EMISSION (route through bounds.count) or doing "
        "line arithmetic (add to ALLOWED with a reason)?",
    )


# ------------------------------------------- spelling 2: zero means unset
def test_no_unreviewed_zero_means_unset():
    """`cap = n or DEFAULT` cannot tell 0 from absent.

    Three confirmed defects: `ctx gc --retention-days 0`, `ctx ask --depth 0`
    and `ctx search --max-matches 0` all silently became the configured
    default. bounds.explicit answers "was it given"; `or` answers "is it
    truthy", and for a bound those are different questions.
    """
    _assert_reviewed(
        _zero_means_unset_hits(), ALLOWED_OR, "`bound or DEFAULT`",
        "Use bounds.explicit(raw, default) so an explicit 0 is an answer, "
        "or add the file to ALLOWED_OR with a reason.",
    )


# ------------------------------------ spelling 3: a negative-index window
def test_no_unreviewed_negative_index_windows():
    """`xs[-n:]` is `xs[0:]` at n == 0 -- the whole list.

    `ctx job <id> --tail 0`, an explicit request for no live tail, dumped the
    entire spool. Same class as max(1, n), different spelling, which is
    exactly why this test needed more than one.
    """
    _assert_reviewed(
        _negative_slice_hits(), ALLOWED_NEG_SLICE, "`xs[-n:]` window",
        "Take the tail as xs[len(xs) - bounds.count(n):] (and [] at zero), "
        "or add the file to ALLOWED_NEG_SLICE with a reason.",
    )


# ------------------------------------------------ detection is AST-based
def test_detection_ignores_prose():
    """The textual version matched `max(1, n)` inside a docstring EXPLAINING
    the defect. A grep does not know what code is; this one parses."""
    src = 'X = "max(1, n)"\n# max(1, n)\ndef f():\n    """max(1, n)"""\n'
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "max"
    ]
    assert calls == [], "only real calls count"


# --------------------------------------- zero must be honoured AND safe
def test_caller_supplied_bounds_honour_zero():
    from ctx import bounds

    for zero_means_zero in (0, -1, -(10**9), float("nan")):
        assert bounds.count(zero_means_zero) == 0
    assert bounds.count(7) == 7


def test_zero_bounds_do_not_crash_the_swept_sites():
    """The sweep exposed a defensive floor doing load-bearing work.

    `max(1, n_buckets)` looked like it was only widening a request. It was
    also the reason `width = (hi - lo) / n_buckets` never divided by zero.
    Honouring a zero is only half the contract; surviving it is the other.
    """
    from ctx.query import Stream, _stage_histogram

    rows = [{"v": str(i)} for i in range(10)]
    empty = _stage_histogram(None, Stream("records", rows), ["v", "--buckets", "0"])
    assert empty.rows == [], "zero buckets is an empty census"

    normal = _stage_histogram(None, Stream("records", rows), ["v", "--buckets", "3"])
    assert len(normal.rows) == 3, "a real bucket count still works"


def test_zero_is_safe_for_every_swept_bound_shape():
    from ctx import bounds

    assert [1, 2, 3][: bounds.count(0)] == []      # cap / limit sites
    assert min(bounds.count(0), 8) == 0            # depth / tail sites
    xs = [1, 2, 3]
    assert xs[len(xs) - bounds.count(0):] == []    # tail-window sites
