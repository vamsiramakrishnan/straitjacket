"""Acceptance: M-A explorer agent — shipped definition, ephemeral wrap
install, persistent plugin render.

No real agent is ever launched; wrap_claude is exercised against a fake
`claude` shell script placed on PATH (same style as test_wrap.py).
"""

import os
import stat
from pathlib import Path

from conftest import make_ws

AGENT_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "antigravity"
    / "agents"
    / "ctx-explorer.md"
)


def _frontmatter_and_body(text: str) -> tuple[str, str]:
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", 2)
    return frontmatter, body


def test_agent_definition_frontmatter():
    text = AGENT_TEMPLATE.read_text(encoding="utf-8")
    frontmatter, _ = _frontmatter_and_body(text)
    assert "name: ctx-explorer" in frontmatter
    assert "description:" in frontmatter
    tools_line = next(
        line for line in frontmatter.splitlines() if line.startswith("tools:")
    )
    for tool in ("Bash", "Read", "Grep", "Glob"):
        assert tool in tools_line


def test_agent_definition_protocol():
    _, body = _frontmatter_and_body(AGENT_TEMPLATE.read_text(encoding="utf-8"))
    # Checkpoint-shaped reporting with negatives.
    assert "checkpoint" in body.lower()
    assert "goal:" in body
    assert "findings:" in body
    assert "negative" in body
    # Cite-don't-quote by handle + coordinate; harness verbs over raw reads.
    assert "run:<id>#stdout" in body
    assert "never re-quote more than 3 lines" in body.lower()
    for verb in ("ctx run", "ctx search", "ctx get", "ctx stats"):
        assert verb in body
    assert "wholesale" in body


def _install_fake_claude(bin_dir: Path, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_wrap_claude_installs_and_removes_agent(tmp_path, monkeypatch):
    from ctx.wrap import wrap_claude

    ws = tmp_path / "proj"
    ws.mkdir()
    agent_copy = tmp_path / "agent_seen.md"
    _install_fake_claude(
        tmp_path / "bin",
        f"""\
if [ "$1" = "--help" ]; then
  echo "usage: claude [--settings <file>] [prompt]"
  exit 0
fi
cat .claude/agents/ctx-explorer.md > {agent_copy}
exit 0
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    rc = wrap_claude(ws, [], "/opt/bin/ctx")
    assert rc == 0

    # The agent file was live during the run, byte-identical to the template ...
    assert agent_copy.read_bytes() == AGENT_TEMPLATE.read_bytes()
    # ... and removed afterwards along with the directories we created.
    assert not (ws / ".claude").exists()


def test_wrap_claude_preserves_preexisting_agent(tmp_path, monkeypatch):
    from ctx.wrap import wrap_claude

    ws = tmp_path / "proj"
    agents_dir = ws / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    original = b"---\nname: ctx-explorer\n---\nuser's own explorer\n"
    (agents_dir / "ctx-explorer.md").write_bytes(original)

    agent_copy = tmp_path / "agent_seen.md"
    _install_fake_claude(
        tmp_path / "bin",
        f"""\
if [ "$1" = "--help" ]; then
  echo "usage: claude [--settings <file>] [prompt]"
  exit 0
fi
cat .claude/agents/ctx-explorer.md > {agent_copy}
exit 0
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    rc = wrap_claude(ws, [], "/opt/bin/ctx")
    assert rc == 0

    # The user's definition was never overwritten — during or after the run.
    assert agent_copy.read_bytes() == original
    assert (agents_dir / "ctx-explorer.md").read_bytes() == original


def test_render_plugin_copies_agents(workspace_dir):
    from ctx.installer import render_plugin

    dest = render_plugin(workspace_dir, ctx_exe="/opt/bin/ctx")
    installed = dest / "agents" / "ctx-explorer.md"
    assert installed.is_file()
    assert installed.read_bytes() == AGENT_TEMPLATE.read_bytes()
    assert dest == workspace_dir / ".agents" / "plugins" / "ctx-harness"


def test_doctor_reports_explorer_agent(state_home, workspace_dir):
    from ctx.installer import doctor_report, render_plugin

    ws = make_ws(workspace_dir)
    # Plugin absent: informational, not a failure.
    report = doctor_report(ws, antigravity=True)
    assert "✓ explorer agent" in report
    assert "plugin not installed" in report

    render_plugin(workspace_dir, ctx_exe="/opt/bin/ctx")
    report = doctor_report(ws, antigravity=True)
    assert "✓ explorer agent — agents/ctx-explorer.md" in report
