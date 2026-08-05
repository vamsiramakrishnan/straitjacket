"""Acceptance: the guard's own advice must not become the context bloat.

Found by replaying this repo's transcripts (`ctx replay --all-projects`): three
of nine sessions came out WORSE under the harness than without it — the worst
128 -> 439 tokens (-243%). Cause: six denials in one short session each re-sent
the same ~50-token explanation of a lesson the model took on the first call.

So a lesson is spelled out once per session and then abbreviated to its verdict
line. Safety-class outcomes are excluded: rule-7 (tests/test_safety_invariant)
forbids adaptive state from rewording a safety denial, however often it fires.
"""

from __future__ import annotations

from ctx.hook import classify


def _decide(cmd, ws):
    return classify({
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "workspacePaths": [str(ws)],
        "cwd": str(ws),
    })


def _reason(d):
    return (d.get("rewrite") or {}).get("reason") or d.get("reason") or ""


def test_a_repeated_lesson_gets_cheaper(state_home, git_workspace):
    first = _reason(_decide("python3 -c 'print(1)'", git_workspace))
    second = _reason(_decide("python3 -c 'print(2)'", git_workspace))
    assert len(first.split()) > len(second.split()), (first, second)
    assert second.splitlines()[0] == first.splitlines()[0]  # verdict survives


def test_a_new_lesson_is_still_taught_in_full(state_home, git_workspace):
    _decide("python3 -c 'print(1)'", git_workspace)  # teach lesson A
    other = _reason(_decide("find . -name '*.py' -exec cat {} ;", git_workspace))
    assert other, "an unfamiliar shape must still get its explanation"


def test_a_committed_deny_rule_never_reworded(state_home, git_workspace):
    """Safety class: wording frozen no matter how many times it fires."""
    (git_workspace / "ctx.toml").write_text(
        'version = 1\n[guard]\ndeny_commands = ["curl "]\n', encoding="utf-8"
    )
    seen = {_reason(_decide("curl https://example.com/x", git_workspace)) for _ in range(4)}
    assert len(seen) == 1, seen


def test_the_internal_safety_marker_never_reaches_the_host(state_home, git_workspace):
    (git_workspace / "ctx.toml").write_text(
        'version = 1\n[guard]\ndeny_commands = ["curl "]\n', encoding="utf-8"
    )
    assert "_safety" not in _decide("curl https://example.com/x", git_workspace)
