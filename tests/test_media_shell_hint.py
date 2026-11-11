"""Video on Claude Code: the hint rides the model's own ffmpeg/ffprobe call.

Claude Code 2.1.322 refuses Read on a binary extension in its input
validation, before PreToolUse runs, and its hooks reference says such
validation rejections fire neither PreToolUse nor PostToolUseFailure
(observed live too). The first call a hook sees is the model improvising with
ffmpeg in the shell, so that is where the one-line `ctx view` hint goes.
"""

from __future__ import annotations

import json

import pytest

from media_headers import mp4_header
from test_media_read import hso, ledger, pre, ws  # noqa: F401  (ws is a fixture)


def bash(ws, command, monkeypatch, capsys, flavor="claude-code"):
    return pre(ws, "Bash", {"command": command}, monkeypatch, capsys, flavor)


def said(out: dict) -> str:
    h = hso(out)
    return (str(h.get("permissionDecisionReason", "")) + str(h.get("additionalContext", ""))
            + str(out.get("reason", "")))


def test_ffprobe_on_a_video_names_ctx_view(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "clip.mp4").write_bytes(mp4_header())
    text = said(bash(ws, "ls && ffprobe -v error clip.mp4", monkeypatch, capsys))
    assert "CTX_MEDIA: to look at this video, run `ctx view clip.mp4`" in text
    assert ledger(ws)[-1]["note"] == "media:shell_hint"


def test_a_routed_ffmpeg_call_carries_the_hint_as_context(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "clip.mp4").write_bytes(mp4_header())
    out = bash(ws, "mkdir -p f && ffmpeg -i clip.mp4 f/%02d.png", monkeypatch, capsys)
    h = hso(out)
    if "updatedInput" in h:  # rewritten through ctx run: the hint is model context
        assert "ctx view clip.mp4" in h["additionalContext"]
    else:
        assert "ctx view clip.mp4" in said(out)


@pytest.mark.parametrize("setup,command", [
    ("plain", "ls clip.mp4"),                       # no ffmpeg: untouched
    ("text", "ffprobe notes.mp4"),                  # not a container
    ("secret", "ffprobe secrets/clip.mp4"),
    ("off", "ffprobe clip.mp4"),
    ("outside", "ffprobe ../elsewhere.mp4"),
    ("word", "echo ffmpeg clip.mp4"),                # ffmpeg is an argument, not the program
    ("word", "grep ffmpeg notes clip.mp4"),
])
def test_shell_hint_gates(ws, monkeypatch, capsys, setup, command):  # noqa: F811
    (ws / "clip.mp4").write_bytes(mp4_header())
    (ws / "notes.mp4").write_text("ftyp prose\n")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "clip.mp4").write_bytes(mp4_header())
    (ws / "a`id`.mp4").write_bytes(mp4_header())
    (ws.parent / "elsewhere.mp4").write_bytes(mp4_header())
    if setup == "off":
        (ws / "ctx.toml").write_text('[media]\nvideo_read = "off"\n')
    assert "CTX_MEDIA" not in said(bash(ws, command, monkeypatch, capsys))


@pytest.mark.parametrize("command", [
    "bash -c 'ffmpeg -i sub/v.mp4 x.png'",
    "cd sub && ffprobe v.mp4",
    "FOO=1 timeout 30 ffmpeg -i sub/v.mp4 -f null -",
])
def test_the_program_is_found_through_shells_cd_and_wrappers(ws, monkeypatch, capsys, command):  # noqa: F811
    (ws / "sub").mkdir()
    (ws / "sub" / "v.mp4").write_bytes(mp4_header())
    assert "`ctx view sub/v.mp4`" in said(bash(ws, command, monkeypatch, capsys))


def test_once_per_file_per_session(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "clip.mp4").write_bytes(mp4_header())
    assert "CTX_MEDIA" in said(bash(ws, "ffprobe clip.mp4", monkeypatch, capsys))
    assert "CTX_MEDIA" not in said(bash(ws, "ffprobe -v error clip.mp4", monkeypatch, capsys))


def test_no_hint_when_ctx_view_cannot_produce_anything(ws, monkeypatch, capsys):  # noqa: F811
    from ctx import mediaread

    (ws / "clip.mp4").write_bytes(mp4_header())
    monkeypatch.setattr(mediaread, "has_pillow", lambda: False)
    assert "CTX_MEDIA" not in said(bash(ws, "ffprobe clip.mp4", monkeypatch, capsys))
    monkeypatch.setattr(mediaread, "has_pillow", lambda: True)
    monkeypatch.setattr(mediaread, "which", lambda n: None if n == "ffmpeg" else f"/b/{n}")
    assert "CTX_MEDIA" not in said(bash(ws, "ffprobe clip.mp4", monkeypatch, capsys))


def test_an_ask_reaches_the_model_after_the_call_runs(ws, monkeypatch, capsys):  # noqa: F811
    """The ask reason is shown to the USER; if the call is approved and runs,
    PostToolUse gives the model the hint as context, once."""
    import io
    import sys

    from ctx import hook

    (ws / "clip.mp4").write_bytes(mp4_header())
    out = bash(ws, "ffprobe clip.mp4", monkeypatch, capsys)
    h = hso(out)
    # Claude Code: the volume ask is not emitted (it would add a prompt the
    # host does not show), so the hint goes to the model as context at once.
    assert "permissionDecision" not in h and "CTX_MEDIA" in h["additionalContext"]
    payload = {"session_id": "s1", "tool_name": "Bash", "cwd": str(ws),
               "hook_event_name": "PostToolUse",
               "tool_input": {"command": "ffprobe clip.mp4"},
               "tool_response": {"stdout": "duration=6", "stderr": ""}}

    def post():
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        capsys.readouterr()
        hook.main_post_tool_use("claude-code")
        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert "ctx view clip.mp4" not in json.dumps(post())  # once: already delivered


def test_a_deny_carries_the_hint_in_its_reason(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "clip.mp4").write_bytes(mp4_header())
    (ws / "ctx.toml").write_text('[guard]\nunknown_command = "deny"\nsteering = "deny"\n')
    h = hso(bash(ws, "ffprobe clip.mp4", monkeypatch, capsys))
    assert h["permissionDecision"] == "deny" and "ctx view clip.mp4" in h["permissionDecisionReason"]


def test_shell_hint_is_not_given_on_antigravity(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "clip.mp4").write_bytes(mp4_header())
    out = pre(ws, "run_command", {"CommandLine": "ffprobe clip.mp4", "Cwd": str(ws)},
              monkeypatch, capsys, flavor="antigravity")
    assert "CTX_MEDIA" not in json.dumps(out)
