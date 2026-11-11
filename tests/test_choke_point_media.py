"""The Claude Code choke point (hook.claude_code_pre_tool_use_output) on the
real media paths, and the limits of ctx's self-approval.

Media-aware reads attach advice to a Read answer and, as merged, their
emitter answers it with `allow`. On Claude Code only an approval ctx has
proved may leave the hook, so each media path must come out with its advice
and no decision: the advise path, the opt-in redirect (rewritten input, no
decision) and the video hint on a Bash command.
"""

from __future__ import annotations

import json

import pytest

from ctx import hook
from media_headers import mp4_header
from test_media_read import _redirect_ws, hso, png_header, pre, read, ws  # noqa: F401


def _raw_and_gated(ws_path, tool, tool_input):
    """The decision the classifier makes, what the merged emitter makes of it
    with no gate in front (the media package's own allow), and the gated one."""
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(ws_path), "session_id": "s1"}
    policy = hook._load_guard_policy(str(ws_path))
    policy["_flavor"] = "claude-code"
    policy["_output_substitution"] = True
    decision = hook.classify(payload, policy)
    raw = hook._to_claude_code_schema({**decision, "_permission": "allow"})
    gated = hook._claude_permission_gate(decision, payload, str(ws_path))
    return raw, gated


def test_advise_path_keeps_the_advice_and_drops_the_allow(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "screen.png").write_bytes(png_header(2560, 1440))
    out = hso(read(ws, ws / "screen.png", monkeypatch, capsys))
    assert "permissionDecision" not in out and "updatedInput" not in out
    assert out["additionalContext"].startswith("CTX_MEDIA:")
    raw, gated = _raw_and_gated(ws, "Read", {"file_path": str(ws / "screen.png")})
    assert raw["hookSpecificOutput"]["permissionDecision"] == "allow"  # the merged answer
    final = hook.claude_code_pre_tool_use_output(raw, gated)
    assert "permissionDecision" not in final["hookSpecificOutput"]
    assert final["hookSpecificOutput"]["additionalContext"] == out["additionalContext"]


def test_redirect_path_rewrites_without_a_decision(ws, monkeypatch, capsys):  # noqa: F811
    shot = _redirect_ws(ws)
    out = hso(read(ws, shot, monkeypatch, capsys))
    assert "permissionDecision" not in out
    assert ".ctx-session-reads/media/overview-" in out["updatedInput"]["file_path"]
    assert "REDUCED OVERVIEW" in out["additionalContext"]
    raw, gated = _raw_and_gated(ws, "Read", {"file_path": str(shot)})
    raw["hookSpecificOutput"]["permissionDecision"] = "allow"  # as if no _no_approve
    final = hook.claude_code_pre_tool_use_output(raw, gated)["hookSpecificOutput"]
    assert "permissionDecision" not in final and "updatedInput" in final and "additionalContext" in final


def test_video_hint_on_bash_keeps_the_hint_and_has_no_decision(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "clip.mp4").write_bytes(mp4_header())
    out = hso(pre(ws, "Bash", {"command": "ffprobe -v error clip.mp4"}, monkeypatch, capsys))
    assert "permissionDecision" not in out
    assert "ctx view clip.mp4" in out["additionalContext"]


# ------------------------------------------------- a. Read rules bind ctx verbs
@pytest.mark.parametrize("kind", ["deny", "ask"])
@pytest.mark.parametrize("command", [
    "ctx get secrets/key.txt", "ctx search token", "ctx refs Token", "ctx diff run:a run:b",
])
def test_read_rules_withdraw_self_approval(tmp_path, monkeypatch, kind, command):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {kind: ["Read(./secrets/**)"]}}))
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}}
    assert hook._ctx_self_approved(payload, str(tmp_path)) is False
    (home / ".claude" / "settings.json").write_text("{}")
    assert hook._ctx_self_approved(payload, str(tmp_path)) is True


# ------------------------------------------------- b. only one plain ctx call
@pytest.mark.parametrize("command", [
    "PYTHONPATH=. ctx get x", "env LD_PRELOAD=./x.so ctx stats", "ctx get x > ~/.bashrc",
    "./ctx get x", "/usr/local/bin/ctx get x", "ctx get x; ls", "ctx get x\nls",
    "ctx get $(rm -rf y)", "ctx get 'x'", "command ctx get x", "ctx run -- ls",
])
def test_no_prefixed_redirected_or_path_qualified_call_is_self_approved(tmp_path, monkeypatch, command):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}}
    assert hook._ctx_self_approved(payload, str(tmp_path)) is False
