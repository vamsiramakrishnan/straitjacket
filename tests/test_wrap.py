"""Acceptance: `ctx wrap` — ephemeral Claude Code wrap, persistent Antigravity wrap.

No real agent is ever launched; wrap_claude is exercised against a fake
`claude` shell script placed on PATH.
"""

import json
import os
import stat
from pathlib import Path

import pytest


def test_prepare_claude_settings_shape(tmp_path):
    from ctx.wrap import prepare_claude

    settings = prepare_claude(tmp_path, "/opt/bin/ctx")
    json.dumps(settings)  # must be JSON-serializable
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit"
    hook = entry["hooks"][0]
    assert hook["type"] == "command"
    assert hook["timeout"] == 10
    assert "hook claude-code pre-tool-use" in hook["command"]
    assert hook["command"].startswith("/opt/bin/ctx")

    # PostToolUse: the universal emission gate covers every faucet that emits
    # into the window (incl. MCP), excludes tiny status tools, and runs in
    # Python (the Rust shim can't digest).
    post = settings["hooks"]["PostToolUse"][0]
    assert post["matcher"] == "Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*"
    assert "Edit" not in post["matcher"] and "Write" not in post["matcher"]
    post_cmd = post["hooks"][0]["command"]
    assert post_cmd == "/opt/bin/ctx hook claude-code post-tool-use"


def test_prepare_claude_default_exe_is_absolute_or_module(tmp_path):
    from ctx.installer import _ctx_executable
    from ctx.wrap import prepare_claude

    cmd = prepare_claude(tmp_path, _ctx_executable())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert os.path.isabs(cmd.split()[0]) or "-m ctx" in cmd


def test_print_config_claude():
    from ctx.wrap import print_config

    out = print_config("claude", "/opt/bin/ctx")
    settings = json.loads(out)
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd == "/opt/bin/ctx hook claude-code pre-tool-use"


def test_print_config_antigravity():
    from ctx.wrap import print_config

    out = print_config("antigravity", "/opt/bin/ctx")
    assert "ctx antigravity install" in out
    assert "ctx doctor --antigravity" in out


def test_print_config_unknown_host():
    from ctx.wrap import print_config

    with pytest.raises(ValueError, match="unsupported wrap host"):
        print_config("cursor")


def test_output_discipline_injected_in_print_mode(monkeypatch):
    from ctx.wrap import _with_output_discipline

    monkeypatch.delenv("CTX_WRAP_NO_DISCIPLINE", raising=False)
    args = _with_output_discipline(["-p", "fix it"])
    assert args[0] == "--append-system-prompt"
    assert "Output discipline" in args[1]
    assert args[-2:] == ["-p", "fix it"]
    # --print spelling counts too
    assert _with_output_discipline(["--print", "x"])[0] == "--append-system-prompt"


def test_output_discipline_not_injected_interactive_or_opted_out(monkeypatch):
    from ctx.wrap import _with_output_discipline

    monkeypatch.delenv("CTX_WRAP_NO_DISCIPLINE", raising=False)
    assert _with_output_discipline([]) == []  # interactive: untouched
    own = ["--append-system-prompt", "mine", "-p", "x"]
    assert _with_output_discipline(own) == own  # user's prompt wins
    monkeypatch.setenv("CTX_WRAP_NO_DISCIPLINE", "1")
    assert _with_output_discipline(["-p", "x"]) == ["-p", "x"]  # env opt-out


def _install_fake_claude(bin_dir: Path, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_wrap_claude_via_settings_flag(tmp_path, monkeypatch):
    from ctx.wrap import wrap_claude

    ws = tmp_path / "proj"
    ws.mkdir()
    argv_file = tmp_path / "argv.txt"
    settings_copy = tmp_path / "settings_copy.json"
    _install_fake_claude(
        tmp_path / "bin",
        f"""\
if [ "$1" = "--help" ]; then
  echo "usage: claude [--settings <file>] [prompt]"
  exit 0
fi
printf '%s\\n' "$@" > {argv_file}
prev=""
for a in "$@"; do
  if [ "$prev" = "--settings" ]; then cat "$a" > {settings_copy}; fi
  prev="$a"
done
exit 7
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    rc = wrap_claude(ws, ["-p", "fix the failing test"], "/opt/bin/ctx")
    assert rc == 7  # agent exit code propagated

    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv[-2:] == ["-p", "fix the failing test"]
    assert "--settings" in argv
    settings_path = argv[argv.index("--settings") + 1]

    # The settings file held the hooks JSON while the agent ran ...
    seen = json.loads(settings_copy.read_text(encoding="utf-8"))
    cmd = seen["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd == "/opt/bin/ctx hook claude-code pre-tool-use"
    # ... and was removed afterwards: zero residue.
    assert not os.path.exists(settings_path)
    assert not (ws / ".claude").exists()


def test_wrap_claude_fallback_merge_restores_settings(tmp_path, monkeypatch):
    from ctx.wrap import wrap_claude

    ws = tmp_path / "proj"
    (ws / ".claude").mkdir(parents=True)
    original = '{"model": "opus"}'
    (ws / ".claude" / "settings.json").write_text(original, encoding="utf-8")

    merged_copy = tmp_path / "merged.json"
    # --help does not advertise --settings → merge fallback path.
    _install_fake_claude(
        tmp_path / "bin",
        f"""\
if [ "$1" = "--help" ]; then echo "usage: claude [prompt]"; exit 0; fi
cat .claude/settings.json > {merged_copy}
exit 7
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    rc = wrap_claude(ws, [], "/opt/bin/ctx")
    assert rc == 7
    merged = json.loads(merged_copy.read_text(encoding="utf-8"))
    assert merged["model"] == "opus"  # existing keys preserved during the run
    assert "hook claude-code pre-tool-use" in json.dumps(merged["hooks"])
    # Restored byte-exactly afterwards.
    assert (ws / ".claude" / "settings.json").read_text(encoding="utf-8") == original


def test_wrap_claude_missing_agent(tmp_path, monkeypatch, capsys):
    from ctx.wrap import wrap_claude

    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    rc = wrap_claude(tmp_path, [], "/opt/bin/ctx")
    assert rc == 127
    err = capsys.readouterr().err
    assert "not found on PATH" in err


def test_wrap_antigravity_installs_plugin(state_home, workspace_dir, capsys):
    from ctx.wrap import wrap_antigravity

    rc = wrap_antigravity(workspace_dir)
    assert rc == 0
    assert (workspace_dir / ".agents" / "plugins" / "ctx-harness" / "plugin.json").is_file()
    out = capsys.readouterr().out
    assert "installed plugin" in out
    assert "persistent" in out  # asymmetry vs the ephemeral claude wrap


def test_cli_wrap_print_config(capsys):
    from ctx.cli import main

    rc = main(["wrap", "claude", "--print-config"])
    assert rc == 0
    settings = json.loads(capsys.readouterr().out)
    assert "hook claude-code pre-tool-use" in json.dumps(settings)
