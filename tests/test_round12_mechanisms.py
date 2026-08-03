"""Seven defects from bug-bash round 12, pinned as mechanisms.

Round 11 had declared ZERO on nearly the same tree -- while hitting its turn
cap mid-probe at 70 tool calls. Raising the cap to 90 produced these seven
at precision 1.0, which settles what that zero meant: a truncated arm and a
clean one are indistinguishable from the outside, and only one of them is
evidence.
"""

from __future__ import annotations

import pytest
from conftest import make_store, make_ws


# --------------------------- an optional engine is a SPEED choice only
def test_both_reachability_engines_agree(state_home, tmp_path):
    """callgraph's docstring promises "identical results, so the engine is a
    speed choice, not a semantic one". The stdlib BFS excluded any reachable
    node that was itself a seed and networkx did not, so `ctx impact` on an
    ambiguous symbol returned a different blast radius depending on whether
    networkx happened to be importable."""
    from ctx.callgraph import _load_graph, _reachable, _resolve_target

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text(
        "def shared():\n    return 1\n\n\ndef top():\n    return shared()\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "from a import shared\n\n\ndef shared_caller():\n    return shared()\n",
        encoding="utf-8",
    )
    ws = make_ws(tmp_path)
    g = _load_graph(make_store(ws), ws)
    seeds = _resolve_target(g, "shared")

    import ctx.callgraph as cg

    with_nx = _reachable(g, seeds, 6, True)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _no_networkx(name, *a, **k):
        if name == "networkx":
            raise ImportError("forced off")
        return real_import(name, *a, **k)

    import builtins

    builtins.__import__ = _no_networkx
    try:
        without_nx = _reachable(g, seeds, 6, True)
    finally:
        builtins.__import__ = real_import

    assert with_nx == without_nx, (
        f"engines disagree: networkx={sorted(with_nx)} stdlib={sorted(without_nx)}"
    )
    assert cg is not None


# --------------------- an explicit horizon outranks a default policy
def test_explicit_retention_zero_collects(state_home, workspace_dir):
    """`ctx gc --retention-days 0` is documented as "collect everything
    already expired" and left freshly-written manifests alive behind the
    30-day lease they had minted for themselves moments earlier -- the
    user's explicit horizon losing to the default it was written to
    override."""
    import sys

    from ctx.execution import run_capture
    from ctx.store import UnknownIdError

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = run_capture(ws, [sys.executable, "-c", "print('x')"], store=store)
    store.gc(0, override_retention=True)
    with pytest.raises(UnknownIdError):
        store.get_manifest(cap.manifest_id)


def test_an_implicit_horizon_still_honours_retention_leases(state_home, workspace_dir):
    """The other half, and the reason this is a keyword rather than a
    behaviour change: the DEFAULT path must keep honouring a retention lease
    past the recency cutoff, which tests/test_pr1_review_fixes.py pins."""
    import sys

    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = run_capture(ws, [sys.executable, "-c", "print('x')"], store=store)
    with store.db:
        store.db.execute("UPDATE objects SET created_at = 0")
    store.gc(1)  # implicit: no override
    assert store.get_manifest(cap.manifest_id)["schema"] == "ctx.invocation/v1"


def test_a_pin_survives_even_an_explicit_zero(state_home, workspace_dir):
    """Pins are protection someone ASKED for, not a default being retuned."""
    import sys

    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = run_capture(ws, [sys.executable, "-c", "print('x')"], store=store)
    store.pin(cap.manifest_id)
    store.gc(0, override_retention=True)
    assert store.get_manifest(cap.manifest_id)["schema"] == "ctx.invocation/v1"


# ------------------------- a span addresses the stream it names
def test_mint_span_refuses_out_of_range_regions(state_home, workspace_dir):
    """The guard lives at the one door every profile mints through: two
    profiles were passing coordinates that were not line numbers in the
    stream they named, and neither produced an error -- just a span
    resolving to unrelated text. An out-of-range region is refused, so a
    digest loses one address instead of gaining a wrong one."""
    import sys

    from ctx.digest.base import DigestContext
    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = run_capture(
        ws, [sys.executable, "-c", "print('a');print('b');print('c')"], store=store
    )
    ctx = DigestContext.load(store, ws, cap.manifest, focus=None)
    assert ctx.mint_span(ctx.stdout, "region", a=1, b=3) is not None
    assert ctx.mint_span(ctx.stdout, "region", a=1, b=9999) is None
    assert ctx.mint_span(ctx.stdout, "region", a=0, b=2) is None
    assert ctx.mint_span(ctx.stdout, "region", a=3, b=1) is None


def test_search_matches_carry_their_stdout_line():
    """The span for "the rest of the matches" was minted from the match
    ORDINAL, which equals the stdout line only when every line is a match --
    false the moment grep prints -A/-B/-C context."""
    from ctx.digest.searchprof import _parse

    lines = [
        "a.py:1:hit one",
        "a.py-2-context",
        "a.py-3-context",
        "a.py:4:hit two",
    ]
    parsed = _parse(lines)
    assert [p[3] for p in parsed] == [1, 4], "stdout indices, not ordinals"


# ------------------------------ a substitution may not WIDEN the request
def test_grep_collapse_preserves_directory_scope():
    """`grep -rn X tests/` and `grep -rn X` collapsed to the identical
    whole-repo command. The replacement surface may make a search cheaper;
    it may not make it bigger, because the extra results are
    indistinguishable from real ones."""
    from ctx.substitute import _scope_hint

    assert _scope_hint(["tests/"]) == "tests/**"
    assert _scope_hint(["src/ctx/hook.py"]) == "src/ctx/hook.py"
    assert _scope_hint(["*.py"]) == "*.py"
    assert _scope_hint(["."]) == "", "the whole repo needs no narrowing"
    assert _scope_hint([]) == ""
    assert _scope_hint(["a/", "b/"]) is None, "inexpressible: do not substitute"


def test_grep_collapse_declines_rather_than_widening():
    import shlex

    from ctx.substitute import collapse

    sub = collapse(
        "grep -rn NEEDLE dir_a/ dir_b/",
        failure_available=False, symbols_resolvable=False,
    )
    assert sub is None, "two scopes cannot be one --glob; leave it alone"

    scoped = collapse(
        "grep -rn NEEDLE tests/", failure_available=False, symbols_resolvable=False
    )
    assert scoped is not None and "tests/**" in scoped.command
    assert shlex.split(scoped.command)


# ------------------------- a bound must count what was actually spent
def test_escalation_is_charged_at_the_model_that_ran():
    """est_cost_usd is computed once at plan-build time from the originally
    assigned model, so an escalation to a pricier one was never charged and
    budget_usd could be overrun without appearing to be."""
    from ctx.orchestrator import _actual_cost

    class _M:
        def __init__(self, i):
            self.id = i

    class _A:
        est_cost_usd = 0.01

        def __init__(self, mid):
            self.model = _M(mid)

    class _O:
        def __init__(self, esc):
            self.escalated_to = esc

    assert _actual_cost(_A("m1"), _O(None)) == 0.01, "no escalation: unchanged"
    assert _actual_cost(_A("m1"), _O("host/m1")) == 0.01, "same model: unchanged"
    assert _actual_cost(_A("m1"), _O("host/other")) > 0.01, (
        "an escalation must cost more than the estimate it replaced"
    )


# ---------------------------------- a coverage line that reconciles
def test_template_coverage_is_not_understated():
    """mine_templates puts every mined line in exactly ONE template, so all N
    templates always cover all M lines. The line reported only the DISPLAYED
    ten's count as the coverage, implying M-C lines had no identified
    template -- a number that could never be reconciled with the rows under
    it."""
    import re
    import sys

    from ctx.digest.base import DigestContext
    from ctx.digest.logprof import LogTemplateProfile
    from ctx.execution import run_capture

    ws = make_ws(pytest.importorskip("pathlib").Path(_tmpdir()))
    store = make_store(ws)
    script = (
        "import sys\n"
        "for k in range(14):\n"
        "    for i in range(3):\n"
        "        sys.stdout.write(f'evt{k} id={i} done\\n')\n"
    )
    cap = run_capture(ws, [sys.executable, "-c", script], store=store)
    ctx = DigestContext.load(store, ws, cap.manifest, focus=None)
    prof = LogTemplateProfile()
    if not prof.detect(ctx):
        pytest.skip("profile did not claim this shape")
    m = re.search(r"templates: ([\d,]+) cover ([\d,]+)/([\d,]+) lines", prof.render(ctx))
    assert m, "the coverage line must still be present"
    assert m.group(2) == m.group(3), "every mined line has a template"


def _tmpdir():
    import pathlib
    import tempfile

    d = pathlib.Path(tempfile.mkdtemp())
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return d
