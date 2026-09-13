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


def test_single_shot_notice_injected_in_print_mode(monkeypatch):
    """evals/bugbash-round17-2026-09-04.md: ScheduleWakeup told a print-mode
    main agent "the harness re-invokes you" and it ended its turn waiting on
    7 background subagents, which print mode then killed. Every print-mode
    launch must be told plainly that no such re-invocation happens here."""
    from ctx.wrap import _SINGLE_SHOT_NOTICE, _with_output_discipline

    monkeypatch.delenv("CTX_WRAP_NO_DISCIPLINE", raising=False)
    args = _with_output_discipline(["-p", "fix it"])
    assert args[0] == "--append-system-prompt"
    assert _SINGLE_SHOT_NOTICE in args[1]
    assert "Output discipline" in args[1]  # both share the one system-prompt slot


def test_single_shot_notice_not_injected_interactive_or_opted_out(monkeypatch):
    from ctx.wrap import _SINGLE_SHOT_NOTICE, _with_output_discipline

    monkeypatch.delenv("CTX_WRAP_NO_DISCIPLINE", raising=False)
    # Interactive: no system prompt injected at all, so no notice either.
    assert _with_output_discipline([]) == []
    # A caller's own --append-system-prompt wins outright, notice included.
    own = ["--append-system-prompt", "mine", "-p", "x"]
    result = _with_output_discipline(own)
    assert result == own
    assert _SINGLE_SHOT_NOTICE not in result[1]
    # The existing opt-out covers the notice too, not just output discipline.
    monkeypatch.setenv("CTX_WRAP_NO_DISCIPLINE", "1")
    result = _with_output_discipline(["-p", "x"])
    assert result == ["-p", "x"]


def test_print_bg_wait_ceiling_defaulted_in_print_mode(monkeypatch):
    from ctx.wrap import _with_print_bg_wait_ceiling

    monkeypatch.delenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", raising=False)
    env = _with_print_bg_wait_ceiling(["-p", "fix it"], None)
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    # --print spelling counts too, and an existing child env is preserved.
    base = {"FOO": "bar"}
    env = _with_print_bg_wait_ceiling(["--print", "x"], base)
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    assert env["FOO"] == "bar"


def test_print_bg_wait_ceiling_not_touched_interactive_or_user_set(monkeypatch):
    from ctx.wrap import _with_print_bg_wait_ceiling

    monkeypatch.delenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", raising=False)
    # Interactive session: no ceiling to fight, leave env untouched.
    assert _with_print_bg_wait_ceiling([], None) is None
    assert _with_print_bg_wait_ceiling([], {"FOO": "bar"}) == {"FOO": "bar"}
    # The user's own setting wins, whether in the passed env or the process env.
    own = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "5000"}
    assert _with_print_bg_wait_ceiling(["-p", "x"], own) == own
    monkeypatch.setenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "5000")
    assert _with_print_bg_wait_ceiling(["-p", "x"], None) is None


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


def test_wrap_claude_sets_print_bg_wait_ceiling_for_print_mode(tmp_path, monkeypatch):
    from ctx.wrap import wrap_claude

    monkeypatch.delenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", raising=False)
    ws = tmp_path / "proj"
    ws.mkdir()
    env_file = tmp_path / "env.txt"
    _install_fake_claude(
        tmp_path / "bin",
        f"""\
if [ "$1" = "--help" ]; then
  echo "usage: claude [--settings <file>] [prompt]"
  exit 0
fi
printenv CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS > {env_file} 2>&1 || true
exit 0
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    rc = wrap_claude(ws, ["-p", "fix the failing test"], "/opt/bin/ctx")
    assert rc == 0
    assert env_file.read_text(encoding="utf-8").strip() == "0"


def test_wrap_claude_leaves_print_bg_wait_ceiling_untouched_if_user_set(tmp_path, monkeypatch):
    from ctx.wrap import wrap_claude

    ws = tmp_path / "proj"
    ws.mkdir()
    env_file = tmp_path / "env.txt"
    _install_fake_claude(
        tmp_path / "bin",
        f"""\
if [ "$1" = "--help" ]; then
  echo "usage: claude [--settings <file>] [prompt]"
  exit 0
fi
printenv CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS > {env_file} 2>&1 || true
exit 0
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "5000")

    rc = wrap_claude(ws, ["-p", "fix the failing test"], "/opt/bin/ctx")
    assert rc == 0
    assert env_file.read_text(encoding="utf-8").strip() == "5000"


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


# ---------------------------------------------------------------- proxy env
# Claude Code turns deferred tool loading off for any non-first-party
# ANTHROPIC_BASE_URL. The observer proxy is a byte-for-byte relay, so when its
# upstream is Anthropic itself the wrapper must say so, or every request pays
# the full tool catalogue (measured: ~15k cached tokens per call, 41 inline
# tool schemas instead of 16).
def test_proxy_child_env_keeps_tool_deferral_for_first_party_upstream(monkeypatch):
    from ctx.wrap import _proxy_child_env

    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    env = _proxy_child_env(4242, "https://api.anthropic.com")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4242"
    assert env["ENABLE_TOOL_SEARCH"] == "true"
    assert "ENABLE_TOOL_SEARCH" not in os.environ  # parent untouched


def test_proxy_child_env_respects_a_user_choice(monkeypatch):
    from ctx.wrap import _proxy_child_env

    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "auto:3")
    assert _proxy_child_env(4242, "https://api.anthropic.com")["ENABLE_TOOL_SEARCH"] == "auto:3"
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    assert _proxy_child_env(4242, "https://api.anthropic.com")["ENABLE_TOOL_SEARCH"] == "false"


def test_proxy_child_env_never_forces_a_third_party_gateway(monkeypatch):
    from ctx.wrap import _proxy_child_env

    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    for upstream in ("http://127.0.0.1:9", "https://gateway.example.com/v1", "not a url"):
        assert "ENABLE_TOOL_SEARCH" not in _proxy_child_env(4242, upstream), upstream


# ---------------------------------------------------------- prefix parity
# The manifest locks the bytes the harness injects; the probe measures the
# bytes the host adds because of the harness. Pure parts tested here; the two
# live single-turn sessions need a `claude` and credentials.
def test_judge_prefix_parity_passes_within_declared_budget():
    from ctx.wrap import judge_prefix_parity

    naive = {"system_bytes": 14600, "tools": 18, "tools_bytes": 164400, "deferral": True, "tool_names": ["Bash", "Grep", "ToolSearch"]}
    wrapped = {"system_bytes": 15900, "tools": 16, "tools_bytes": 157700, "deferral": True, "tool_names": ["Bash", "ToolSearch"]}
    v = judge_prefix_parity(naive, wrapped, declared_bytes=3505)
    assert v["ok"] and v["delta_bytes"] < 0 and v["tools_only_naive"] == ["Grep"]


def test_judge_prefix_parity_fails_on_lost_deferral_and_on_tax():
    from ctx.wrap import judge_prefix_parity, render_prefix_parity

    naive = {"system_bytes": 14600, "tools": 18, "tools_bytes": 164400, "deferral": True, "tool_names": ["ToolSearch"]}
    wrapped = {"system_bytes": 15900, "tools": 41, "tools_bytes": 257500, "deferral": False, "tool_names": ["Monitor", "CronCreate"]}
    v = judge_prefix_parity(naive, wrapped, declared_bytes=3505)
    assert not v["ok"] and v["deferral_lost"] and "lost tool deferral" in v["reason"] and "over naive" in v["reason"]
    text = render_prefix_parity(v)
    assert "FAIL" in text and "deferral off" in text and "Monitor" in text
    # Same catalogue, deferral kept, but 20 KB more prefix than declared: still a tax.
    v2 = judge_prefix_parity(naive, {**naive, "system_bytes": 14600 + 20000}, declared_bytes=3505)
    assert not v2["ok"] and not v2["deferral_lost"]
    assert judge_prefix_parity(None, wrapped, 0)["ok"] is False


def test_judge_prefix_parity_names_a_tax_both_sessions_pay():
    from ctx.wrap import judge_prefix_parity, render_prefix_parity

    # ENABLE_TOOL_SEARCH=false in the environment: both sides inline 41 tools.
    # Relative parity holds (the wrapper added nothing), but the ~15k/call tax
    # is real and must be named, not hidden behind PASS.
    off = {"system_bytes": 14600, "tools": 41, "tools_bytes": 251000, "deferral": False, "tool_names": []}
    v = judge_prefix_parity(off, {**off, "system_bytes": 15500}, declared_bytes=3505)
    assert v["ok"] and v["environment_tax"] and not v["deferral_lost"]
    assert "both sessions inline" in render_prefix_parity(v)
    small = {"system_bytes": 2000, "tools": 6, "tools_bytes": 9000, "deferral": False, "tool_names": []}
    assert judge_prefix_parity(small, small, 3505)["environment_tax"] is False


def test_prompt_snapshot_reads_the_first_request_shape(tmp_path):
    from ctx.wrap import _prompt_snapshot

    proj = tmp_path / "projects" / "-x"
    proj.mkdir(parents=True)
    rows = [
        {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": "abc"}},
        {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": "abcd",
                                              "tools": [{"name": "Bash"}, {"name": "ToolSearch"}]}},
    ]
    (proj / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    snap = _prompt_snapshot(tmp_path)
    assert snap["system_bytes"] == 4 and snap["tools"] == 2 and snap["deferral"] is True
    assert snap["tool_names"] == ["Bash", "ToolSearch"]
    assert _prompt_snapshot(tmp_path / "nowhere") is None


# -------------------------------------------------------------- tool diet
# The fixed half of every request is the tool catalogue. Measured: 16 tools /
# 154 KB / ~33k cached tokens per call by default on a hosted session, 6-8
# tools / 36-50 KB / ~10k with `--tools`. Print mode declares the coding
# surface; interactive sessions and a caller's own `--tools` are untouched.
def test_tool_diet_applies_to_print_mode_only(monkeypatch):
    from ctx.wrap import _PRINT_TOOL_SURFACE, _with_tool_diet

    monkeypatch.delenv("CTX_WRAP_NO_TOOL_DIET", raising=False)
    out = _with_tool_diet(["-p", "fix it"])
    assert out[:2] == ["--tools", ",".join(_PRINT_TOOL_SURFACE)] and out[2:] == ["-p", "fix it"]
    assert "Bash" in _PRINT_TOOL_SURFACE and "Agent" in _PRINT_TOOL_SURFACE  # explorer agent rides Agent
    assert _with_tool_diet(["--resume", "abc"]) == ["--resume", "abc"]  # interactive: untouched
    assert _with_tool_diet(["--tools", "Bash", "-p", "x"]) == ["--tools", "Bash", "-p", "x"]  # caller wins
    monkeypatch.setenv("CTX_WRAP_NO_TOOL_DIET", "1")
    assert _with_tool_diet(["-p", "x"]) == ["-p", "x"]


def test_judge_prefix_parity_credits_the_tool_diet():
    from ctx.wrap import judge_prefix_parity

    # Print-mode diet: 5 tools, no deferred tools left, so no ToolSearch marker.
    # That is a 110 KB saving per request, not a lost-deferral tax.
    naive = {"system_bytes": 14600, "tools": 16, "tools_bytes": 157700, "deferral": True, "tool_names": ["ToolSearch", "Artifact"]}
    diet = {"system_bytes": 15700, "tools": 5, "tools_bytes": 46000, "deferral": False, "tool_names": ["Bash"]}
    v = judge_prefix_parity(naive, diet, declared_bytes=3505)
    assert v["ok"] and not v["deferral_lost"] and v["delta_bytes"] < -100_000
