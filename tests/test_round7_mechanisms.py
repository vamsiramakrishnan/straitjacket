"""Seven defects from bug-bash round 7, pinned as mechanisms.

Two of them (`--max-matches 0`, `--tail 0`) were the SAME class the
ctx.bounds adoption sweep was supposed to have closed. It did not catch them
because the invariant only knew one SPELLING of the class -- `max(1, n)` --
while these were `n or DEFAULT` and `xs[-n:]`. tests/test_bounds_adoption.py
now knows all three; that widening is the real fix, and it immediately found
two more sites nobody had reported.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------- bounds, other spellings
def test_search_max_matches_zero_means_zero(state_home, workspace_dir):
    """`cap = max_matches or DEFAULT` read an explicit 0 as unset (the cap
    silently became 80), and a negative cap reached `matches[:cap]` as a
    SUFFIX slice -- widening the output from the argument whose job is to
    narrow it."""
    from conftest import make_store, make_ws
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "a.py").write_text("NEEDLE\n" * 50, encoding="utf-8")

    zero = search(store, ws, "repo:.", ["NEEDLE"], max_matches=0)
    assert "shown: 0" in zero, zero
    neg = search(store, ws, "repo:.", ["NEEDLE"], max_matches=-5)
    assert "shown: 0" in neg, "a negative cap must never widen"
    some = search(store, ws, "repo:.", ["NEEDLE"], max_matches=3)
    assert "shown: 3" in some


def test_job_tail_zero_shows_nothing(tmp_path):
    """`lines[-tail:]` is `lines[0:]` at zero, so an explicit request for no
    live tail dumped the entire spool."""
    from ctx.jobs import _spool_excerpt, _tail_of

    assert _tail_of(["a", "b", "c"], 0) == []
    assert _tail_of(["a", "b", "c"], 2) == ["b", "c"]
    assert _tail_of(["a", "b", "c"], -1) == []
    assert _tail_of(["a", "b", "c"], 99) == ["a", "b", "c"]

    spool = tmp_path / "spool.log"
    spool.write_text("".join(f"line{i}\n" for i in range(200)), encoding="utf-8")
    assert _spool_excerpt(spool, 2, 0) == ["line0", "line1", "... (198 lines omitted) ..."]


def test_plan_step_cap_zero_fans_out_over_nothing():
    """`step.cap or max_fanout` turned the narrowest possible request into
    the widest possible fan-out."""
    from ctx import bounds

    assert bounds.count(bounds.explicit(0, 8)) == 0
    assert bounds.count(bounds.explicit(None, 8)) == 8
    assert bounds.count(bounds.explicit(-3, 8)) == 0


def test_short_path_never_returns_the_full_path():
    """The elision widening its own output: `p[-n:]` at n == 0 is the whole
    string, so a width too small to leave room for a tail returned everything
    from the function whose job is to shorten it."""
    from ctx.wrap import _short_path

    long = "/very/long/managed/venv/path/that/should/be/elided/bin/python"
    for width in range(1, 40):
        out = _short_path(long, width)
        assert len(out) <= max(width, len(out)) and out != long or len(long) <= width, (
            f"width={width} returned {len(out)} chars from {len(long)}"
        )
    assert _short_path(long, 1) != long
    assert _short_path("short", 34) == "short"


# ------------------------------------------------------ boundaries, again
def test_provider_prefix_needs_its_delimiter():
    """A server called `git` absorbed every `mcp__github__*` invocation, so
    `github` looked never-used and `git` looked busy -- and never_used /
    unused_high_authority / excessive-authority are all computed from that
    number."""
    from ctx.surface import _provider_match

    assert _provider_match("mcp__git__status", "git") is True
    assert _provider_match("mcp__git", "git") is True
    assert _provider_match("mcp__github__create_pr", "git") is False
    assert _provider_match("mcp__github__create_pr", "github") is True
    assert _provider_match("mcp.git.status", "git") is True
    assert _provider_match("mcp.github.create_pr", "git") is False


def test_glued_shell_operator_is_still_compound():
    """shlex only splits on whitespace, so `src|wc` arrives as ONE token and
    the exact-token test missed it -- substitution then replaced the compound
    command wholesale and discarded the pipeline stage the caller wrote."""
    import shlex

    from ctx.substitute import _is_compound

    for raw in ("grep -rn x src|wc -l", "grep -rn x src>out", "wc -l<f",
                "a&&b", "a;b"):
        assert _is_compound(shlex.split(raw), raw) is True, raw


def test_operator_inside_a_quoted_argument_is_not_compound():
    """The other direction: shlex has stripped the quotes by the time we see
    the tokens, so the character scan has to run over the raw text OUTSIDE
    quotes -- the only place an operator can operate."""
    import shlex

    from ctx.substitute import _is_compound

    for raw in ("grep 'a|b' f", 'grep "a>b" f', "grep -rn x src"):
        assert _is_compound(shlex.split(raw), raw) is False, raw


# --------------------------------- derived state does not outlive its source
def test_excessive_authority_clears_when_invocations_arrive():
    """probe_surface computes the tag with invocations hardcoded to -1
    (unknown); audit() then joins the real wire-log counts. Nothing
    recomputed the tag, so a tool the audit had just PROVEN was used kept a
    label saying it never was."""
    from ctx.surface import Capability, _with

    cap = Capability(
        id="mcp.github.create_pr", kind="mcp_tool", provider="github",
        source="probe", tokens=100, authority="remote-write",
        activation="always", invocations=-1,
        sensitive_terms=(), leakage=("excessive-authority",), overlaps=(),
        detail="",
    )
    used = _with(cap, invocations=7)
    assert "excessive-authority" not in used.leakage, "the audit disproved it"

    still_unused = _with(cap, invocations=0)
    assert "excessive-authority" in still_unused.leakage, "0 still means never used"


def test_reconciliation_preserves_unrelated_leakage_tags():
    from ctx.surface import Capability, _with

    cap = Capability(
        id="mcp.x.y", kind="mcp_tool", provider="x", source="probe",
        tokens=10, authority="destructive", activation="always", invocations=-1,
        sensitive_terms=(), leakage=("unrelated-domain:payments", "excessive-authority"),
        overlaps=(), detail="",
    )
    out = _with(cap, invocations=3)
    assert out.leakage == ("unrelated-domain:payments",)


# --------------------------------------------------- rendering that agrees
def test_fmt_bytes_promotes_on_the_displayed_value():
    """1023.97 KiB is under the threshold and renders as "1024.0 KiB" once
    rounded -- a unit displaying its own overflow. The check has to run on
    the number the reader will actually see."""
    from ctx.textutil import fmt_bytes

    # 1048570 B is 1023.994 KiB -- under the threshold, and "1024.0 KiB"
    # once rounded to one decimal.
    assert fmt_bytes(1048570) == "1.0 MiB", fmt_bytes(1048570)
    assert fmt_bytes(1048530) == "1.0 MiB", fmt_bytes(1048530)
    # Values that genuinely belong in their unit are untouched.
    assert fmt_bytes(1048000) == "1023.4 KiB"
    assert fmt_bytes(1023) == "1023 B"
    assert fmt_bytes(1024) == "1.0 KiB"
    assert fmt_bytes(0) == "0 B"
    # The invariant behind all of it: no unit ever displays its own overflow.
    for n in (1, 1023, 1024, 1048000, 1048530, 1048570, 1048576,
              1073741820, 10**9, 10**12, 10**15):
        assert "1024." not in fmt_bytes(n), f"{n} -> {fmt_bytes(n)}"


def test_bounded_cuts_at_a_line_boundary_at_index_zero():
    """`if nl > 0` skipped the trim when the only newline inside the budget
    was the first character -- so the documented hard backstop kept a
    mid-line fragment, the one thing it exists to prevent, because a falsy
    index read as "no newline found"."""
    from ctx.textutil import bounded

    text = "\n" + "x" * 400
    out = bounded(text, 5)  # 20 bytes: the cut lands mid-x-run
    body = out.split("\n[ctx:truncated")[0]
    assert body == "", f"must cut back to the boundary, got {body!r}"


# ------------------------------------- a cap declares its own overflow
def test_gateway_note_fits_inside_the_budget_it_announces():
    """The note is part of the output, so it comes OUT of the budget rather
    than being added on top of it. Appending it to a slice already at
    max_bytes made the block announcing a 16,384-byte cap the thing that
    exceeded it -- a bound broken by its own disclosure."""
    from ctx.surface_gateway import _MAX_BACKEND_RESULT_BYTES, _bound_result

    big = {"content": [{"type": "text", "text": "x" * 100_000}]}
    out = _bound_result(big)
    text = out["content"][0]["text"]
    assert len(text.encode("utf-8")) <= _MAX_BACKEND_RESULT_BYTES
    assert "gateway caps proxied floods" in text, "the cut is still declared"


def test_gateway_leaves_a_small_result_untouched():
    from ctx.surface_gateway import _bound_result

    small = {"content": [{"type": "text", "text": "hello"}]}
    assert _bound_result(small)["content"][0]["text"] == "hello"


def test_impact_depth_zero_survives_the_plan_op():
    """ask.py fixed `--depth 0` becoming 3 at ITS layer, and the 0 then
    survived into the compiled plan's step args only to be dropped here,
    where a falsy depth read as "no depth given". Two layers, one flag, the
    second undoing the first."""
    import inspect

    from ctx import plan_ops

    src = inspect.getsource(plan_ops._mk_callgraph_op)
    assert 'args.get("depth") is not None' in src, (
        "a depth of 0 must be distinguishable from no depth at all"
    )
