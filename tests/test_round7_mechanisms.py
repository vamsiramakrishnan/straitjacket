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


def test_bounded_trims_to_a_line_boundary_when_that_costs_a_line():
    """The nicety, when it is cheap: a partial trailing line is trimmed away
    so the preview ends where a line does."""
    from ctx.textutil import bounded

    text = "aaaa\nbbbb\ncccc\ndddd\neeee\n" * 40
    out = bounded(text, 4)  # 16 bytes: lands one char into the fourth line
    body = out.split("\n[ctx:truncated")[0]
    assert body == "aaaa\nbbbb\ncccc", f"trim back a partial line: {body!r}"


def test_bounded_never_trims_away_the_whole_payload():
    """The correction round 8 forced, on the test as much as on the code.

    The first version of THIS test asserted `body == ""` -- it encoded the
    defect as the contract. On newline-sparse content (one long line, or the
    exact-bytes body `--bytes` exists to serve) the last newline inside the
    budget is the HEADER's own, so trimming to it deleted every byte the
    caller asked for while still exiting 0 under a header claiming the full
    range. A line-boundary trim is a readability nicety; it may never be the
    reason a bounded preview previews nothing.
    """
    from ctx.textutil import bounded

    header_then_payload = "[ctx get run:abc#stdout]\nselector: --bytes 1:9999\n" + "x" * 9000
    out = bounded(header_then_payload, 200)
    body = out.split("\n[ctx:truncated")[0]
    assert "x" in body, "the payload must survive its own truncation"
    assert body.count("x") > 100, f"a preview must preview something: {len(body)}"


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


# ------------------------------------------------ round 8: the same doors
def test_backslash_is_literal_inside_single_quotes():
    """sh has no escapes inside single quotes. Treating a backslash as one
    desynchronized the quote tracking on `grep 'a\\' | wc -l`, which then read
    as a bare invocation and had its pipeline stage substituted away -- a
    defect introduced BY the round-7 fix for glued operators, and found by
    round 8 running against it."""
    import shlex

    from ctx.substitute import _is_compound, _unquoted

    raw = "grep -rn 'a\\' src|wc -l"
    assert _is_compound(shlex.split(raw), raw) is True
    assert "|" in _unquoted(raw), "the pipe is outside the quotes"
    assert _unquoted("echo 'a|b'") == "echo "


def test_run_reports_a_missing_program_as_127(state_home, workspace_dir, capsys):
    """docs/CLI.md's exit-code table documents 127 for a program not on PATH,
    and `ctx seq` already mapped the identical ExecutionError to it -- so the
    same failure reported differently depending on which verb you reached it
    through."""
    from ctx.cli import main as cli_main

    rc = cli_main(["--workspace", str(workspace_dir), "run", "--",
                   "ctx-definitely-not-a-real-program-xyz"])
    assert rc == 127, capsys.readouterr().err
