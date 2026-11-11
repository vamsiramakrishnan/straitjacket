"""A self-test asserts the PROPERTY, not one shape that satisfies it.

The property of the Claude Code hook today (rules R1/R2): an unbounded
command is never approved together with a rewritten input, and by default
it reaches the host exactly as written, with the output gate as the bound.
`ctx doctor`'s hook line runs a `pytest -q` payload through the real gate
and choke point and says which of those it saw. (It used to report "deny +
collapsed rewrite", which described retired behaviour.)
"""

from __future__ import annotations

from conftest import make_ws


def _hook_line(ws) -> tuple[bool, str]:
    from ctx.installer import doctor_checks

    for name, ok, detail in doctor_checks(ws):
        if name == "hook classifier":
            return ok, detail
    raise AssertionError("the hook classifier check must always run")


def test_default_runs_as_written_and_says_so(state_home, workspace_dir):
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert ok, detail
    assert "runs as written, no decision" in detail and "output gate" in detail
    for stale in ("collapse", "substitut", "deny +"):
        assert stale not in detail


def test_opt_in_rewrite_is_described(state_home, workspace_dir):
    (workspace_dir / "ctx.toml").write_text('version = 1\n[guard]\nsteering = "rewrite"\n')
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert ok, detail
    assert "rewritten to `ctx run`, no decision" in detail


def test_an_allow_with_a_rewrite_fails(state_home, workspace_dir, monkeypatch):
    """R1 broken somewhere upstream of the choke point must not pass."""
    import ctx.hook as hook

    monkeypatch.setattr(hook, "claude_code_pre_tool_use_output", lambda e, d: {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                               "updatedInput": {"command": "ctx run -- pytest -q"}}})
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert not ok, detail


def test_a_broken_classifier_still_fails(state_home, workspace_dir, monkeypatch):
    import ctx.hook as hook

    def _boom(*_a, **_k):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(hook, "classify", _boom)
    ok, detail = _hook_line(make_ws(workspace_dir))
    assert not ok and "exploded" in detail
