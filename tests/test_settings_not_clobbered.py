"""Acceptance: setup never destroys a host settings file it cannot read.

Every installer that merges into a host settings file reads it, merges ctx's
entries in, and writes the whole object back. That round-trip is only safe
while the read succeeds. When it failed, the old code substituted an empty
dict and wrote that back — turning "your JSON has a typo" into "your
permissions, env, MCP servers and model config are gone", and reporting it as
`wrote .claude/settings.json`.

These tests pin the rule: if ctx cannot read it, ctx does not write it.
"""

from __future__ import annotations

import json

from conftest import make_ws

# A realistic settings file: the things a user would be upset to lose.
_USER_SETTINGS = """{
  "permissions": {"allow": ["Bash(git*)"], "deny": ["Bash(rm*)"]},
  "env": {"MY_TOKEN": "secret"},
  "model": "opus",
  "mcpServers": {"mine": {"command": "my-server"}},
"""  # <- deliberately unterminated: valid prefix, invalid document


def test_claude_setup_refuses_to_overwrite_unparseable_settings(workspace_dir):
    from ctx.installer import install_claude

    path = workspace_dir / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_USER_SETTINGS, encoding="utf-8")

    out = install_claude(make_ws(workspace_dir), init_policy=False)

    assert path.read_text(encoding="utf-8") == _USER_SETTINGS, "clobbered the user's settings"
    assert "did not modify" in out
    assert "not valid JSON" in out
    # and it must not claim to have succeeded
    assert "wrote .claude/settings.json" not in out


def test_claude_setup_refuses_when_settings_is_not_an_object(workspace_dir):
    """`[1, 2]` parses fine and then explodes on merge. Same refusal."""
    from ctx.installer import install_claude

    path = workspace_dir / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2]", encoding="utf-8")

    out = install_claude(make_ws(workspace_dir), init_policy=False)

    assert path.read_text(encoding="utf-8") == "[1, 2]"
    assert "did not modify" in out


def test_codex_setup_refuses_to_overwrite_unparseable_hooks(workspace_dir):
    from ctx.installer import install_codex

    path = workspace_dir / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"hooks": {"PreToolUse": [', encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    out = install_codex(make_ws(workspace_dir), init_policy=False)

    assert path.read_text(encoding="utf-8") == before
    assert "did not modify" in out


def test_antigravity_statusline_refuses_to_overwrite_global_settings(tmp_path):
    """This one writes to the user's *global* ~/.gemini settings, so a clobber
    here costs them their configuration for every project, not just this one."""
    from ctx.installer import install_antigravity_statusline

    path = tmp_path / "settings.json"
    path.write_text('{"theme": "dark",', encoding="utf-8")

    out = install_antigravity_statusline("/usr/bin/ctx", settings_path=path)

    assert path.read_text(encoding="utf-8") == '{"theme": "dark",'
    assert "did not modify" in out


def test_doctor_notices_nothing_is_wrapped(state_home, workspace_dir):
    """The command whose whole job is "is it working?" used to print OK for a
    workspace where the hooks had never been installed."""
    from ctx.installer import doctor_report

    report = doctor_report(make_ws(workspace_dir))

    assert "an agent is wrapped" in report
    assert "nothing is hooked yet" in report
    assert "PROBLEMS FOUND" in report


def test_doctor_reads_the_file_setup_wrote(state_home, workspace_dir):
    from ctx.installer import doctor_report, install_claude

    install_claude(make_ws(workspace_dir), init_policy=False)
    report = doctor_report(make_ws(workspace_dir))

    assert "✓ claude hooks" in report
    assert "✓ an agent is wrapped" in report


def test_doctor_reports_settings_that_stopped_parsing(state_home, workspace_dir):
    """Hooks installed, then the user hand-edited the file and broke it. The
    harness is now silently off; doctor has to say so."""
    from ctx.installer import doctor_report, install_claude

    install_claude(make_ws(workspace_dir), init_policy=False)
    (workspace_dir / ".claude" / "settings.json").write_text("{oops", encoding="utf-8")

    report = doctor_report(make_ws(workspace_dir))

    assert "not valid JSON" in report
    assert "PROBLEMS FOUND" in report


def test_valid_settings_still_merge_and_keep_every_key(workspace_dir):
    """The refusal must not cost us the normal path: a readable file still
    gets ctx's hooks merged in with everything else left intact."""
    from ctx.installer import install_claude

    path = workspace_dir / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "permissions": {"allow": ["Bash(git*)"]},
        "env": {"MY_TOKEN": "secret"},
        "model": "opus",
    }
    path.write_text(json.dumps(original, indent=2), encoding="utf-8")

    install_claude(make_ws(workspace_dir), init_policy=False)

    after = json.loads(path.read_text(encoding="utf-8"))
    for key, value in original.items():
        assert after[key] == value, f"lost {key} during merge"
    assert {"PreToolUse", "PostToolUse"} <= set(after["hooks"])
