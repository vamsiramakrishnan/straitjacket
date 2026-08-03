"""A self-test asserts the PROPERTY, not one shape that satisfies it.

`ctx doctor`'s hook check asserted `decision == "deny"` for a bare
`pytest -q`. That is one of the ways the guard keeps its promise, not the
promise itself: under the replacement surface a recognised loop-shape is
ALLOWED and rewritten into the collapsed `ctx q` op, so the flood is
contained by substitution rather than refusal.

A bug bash found the consequence: having captured one pytest failure
earlier in the session -- an entirely normal thing to have done -- flipped
the probe's decision and made `ctx doctor` report PROBLEMS FOUND while the
guard was behaving exactly as designed. A self-test pinned to a proxy fails
when the implementation finds a better way to keep the promise.
"""

from __future__ import annotations

import pytest
from conftest import make_ws


def _hook_line(ws) -> tuple[bool, str]:
    from ctx.installer import doctor_checks

    for name, ok, detail in doctor_checks(ws):
        if name == "hook classifier":
            return ok, detail
    raise AssertionError("the hook classifier check must always run")


def test_a_refused_flood_passes(state_home, workspace_dir, monkeypatch):
    import ctx.hook as hook

    monkeypatch.setattr(hook, "classify", lambda _p: {"decision": "deny"})
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert ok, detail
    assert "deny" in detail


def test_a_substituted_flood_passes_too(state_home, workspace_dir, monkeypatch):
    """The case that broke it. `allow` plus a rewrite means the unbounded
    command never reaches the terminal as written -- which is the whole
    property -- so the self-test must accept it."""
    import ctx.hook as hook

    monkeypatch.setattr(
        hook, "classify",
        lambda _p: {"decision": "allow",
                    "_rewrite": {"command": "ctx q 'fails'", "reason": "collapsed"}},
    )
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert ok, detail
    assert "collapsed rewrite" in detail, (
        "and it must say HOW the flood was contained, not just that it was"
    )


def test_force_ask_passes(state_home, workspace_dir, monkeypatch):
    import ctx.hook as hook

    monkeypatch.setattr(hook, "classify", lambda _p: {"decision": "force_ask"})
    ok, _ = _hook_line(make_ws(workspace_dir))
    assert ok


def test_a_bare_allow_still_fails(state_home, workspace_dir, monkeypatch):
    """The check must not be widened into uselessness: an unbounded pytest
    allowed through UNCHANGED is the flood the guard exists to stop."""
    import ctx.hook as hook

    monkeypatch.setattr(hook, "classify", lambda _p: {"decision": "allow"})
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert not ok, f"a bare allow is a real problem: {detail}"


def test_a_broken_classifier_still_fails(state_home, workspace_dir, monkeypatch):
    import ctx.hook as hook

    def _boom(_p):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(hook, "classify", _boom)
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert not ok and "exploded" in detail
