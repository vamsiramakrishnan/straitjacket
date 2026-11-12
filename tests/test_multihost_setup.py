"""Acceptance tests for Claude, Codex, and Antigravity host setup contracts."""

import io
import json
import sys
import tomllib

from conftest import make_ws


def _run_hook(fn, payload, flavor):
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        assert fn(flavor=flavor) == 0
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = old_in, old_out


# ------------------------------------------------------------------ Codex install


def test_install_codex_writes_valid_config(workspace_dir):
    from ctx.installer import install_codex

    install_codex(make_ws(workspace_dir))
    cfg = workspace_dir / ".codex" / "config.toml"
    hooks = workspace_dir / ".codex" / "hooks.json"
    agents = workspace_dir / "AGENTS.md"
    assert cfg.is_file() and hooks.is_file() and agents.is_file()

    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["features"]["hooks"] is True
    assert data["mcp_servers"]["ctx-harness"]["args"] == ["mcp", "--bounded-only"]
    assert " " not in data["mcp_servers"]["ctx-harness"]["command"]

    hj = json.loads(hooks.read_text(encoding="utf-8"))
    assert {"PreToolUse", "PostToolUse"} <= set(hj["hooks"])
    cmd = hj["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "hook codex pre-tool-use" in cmd
    assert "<!-- ctx-harness:start -->" in agents.read_text(encoding="utf-8")


def test_install_codex_splits_python_module_fallback(monkeypatch, workspace_dir):
    import ctx.installer as installer

    import ctx.selfcmd as selfcmd

    monkeypatch.setattr(selfcmd, "which_absolute", lambda _name, path=None: None)
    installer.install_codex(make_ws(workspace_dir))

    data = tomllib.loads(
        (workspace_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    server = data["mcp_servers"]["ctx-harness"]
    assert server["command"] == sys.executable
    # -P: the fallback must never import a `ctx` package from Codex's cwd.
    assert server["args"] == ["-P", "-m", "ctx", "mcp", "--bounded-only"]


def test_codex_legacy_banner_does_not_authorize_overwriting_user_settings(
    monkeypatch, workspace_dir
):
    import ctx.installer as installer

    codex_dir = workspace_dir / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "# ctx-harness — straitjacket context containment for Codex CLI.\n"
        "[mcp_servers.ctx-harness]\n"
        f'command = "{sys.executable} -m ctx"\n'
        'args = ["mcp", "--bounded-only"]\n',
        encoding="utf-8",
    )
    import ctx.selfcmd as selfcmd

    monkeypatch.setattr(selfcmd, "which_absolute", lambda _name, path=None: None)

    from ctx.host_config import plan_configuration
    from ctx.mcp_hosts import IntegrationError
    import pytest

    config = codex_dir / "config.toml"
    config.write_text(config.read_text() + '\n[my.settings]\nvalue = "keep"\n')
    before = config.read_bytes()
    plan = plan_configuration(workspace_dir, ["codex"])
    assert plan.blockers
    assert '-command = ' in plan.render()
    with pytest.raises(IntegrationError):
        installer.install_codex(make_ws(workspace_dir))
    assert config.read_bytes() == before


def test_install_codex_idempotent_and_nondestructive(workspace_dir):
    from ctx.installer import install_codex

    (workspace_dir / "AGENTS.md").write_text(
        "# My project\n\nHouse rules stay.\n", encoding="utf-8"
    )
    ws = make_ws(workspace_dir)
    install_codex(ws)
    report2 = install_codex(ws)

    agents = (workspace_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "House rules stay." in agents  # user content preserved
    assert agents.count("<!-- ctx-harness:start -->") == 1  # block not duplicated
    assert "already harnessed" in report2


def test_install_codex_preserves_existing_config(workspace_dir):
    from ctx.installer import install_codex

    cfg = workspace_dir / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    original = '[mcp_servers.other]\ncommand = "x"\n'
    cfg.write_text(original, encoding="utf-8")

    report = install_codex(make_ws(workspace_dir))
    assert original in cfg.read_text(encoding="utf-8")
    data = tomllib.loads(cfg.read_text())
    assert data["mcp_servers"]["other"]["command"] == "x"
    assert data["features"]["hooks"] is True
    assert "wrote .codex/config.toml" in report


# ------------------------------------------------------------------ Claude install


def test_install_claude_merges_settings(workspace_dir):
    from ctx.installer import install_claude

    ws = make_ws(workspace_dir)
    install_claude(ws)
    settings = json.loads(
        (workspace_dir / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert "PreToolUse" in settings["hooks"]
    assert (workspace_dir / ".claude" / "agents" / "ctx-explorer.md").is_file()
    # Status line wired: a command that renders model/context/cost/git.
    assert settings["statusLine"]["type"] == "command"
    assert "statusline claude-code" in settings["statusLine"]["command"]

    report2 = install_claude(ws)
    assert "already harnessed" in report2
    s2 = json.loads(
        (workspace_dir / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert len(s2["hooks"]["PreToolUse"]) == 1  # not duplicated


def test_install_claude_delivers_verb_card(workspace_dir):
    """The teaching surface Claude Code otherwise lacks (evals/ask-diagnose-
    3arm): the shipped question vocabulary is upserted into CLAUDE.md,
    marker-delimited and idempotent."""
    from ctx.installer import install_claude

    ws = make_ws(workspace_dir)
    install_claude(ws)
    cm = workspace_dir / "CLAUDE.md"
    assert cm.is_file()
    text = cm.read_text(encoding="utf-8")
    assert "ctx ask" in text and "ctx q" in text and "--intent" in text
    assert "ctx-harness:start" in text and "ctx-harness:end" in text

    # A user's own CLAUDE.md content is preserved; re-install refreshes the
    # block in place (never duplicates).
    cm.write_text("# My project\n\nHouse rules.\n\n" + text, encoding="utf-8")
    install_claude(ws)
    t2 = cm.read_text(encoding="utf-8")
    assert "# My project" in t2 and "House rules." in t2
    assert t2.count("ctx-harness:start") == 1


_RUN_ONLY = ("--intent diagnose", "fails last | in-changed", "records run:<id>#stdout")


def _card_bullets(text: str) -> list[str]:
    return [b.replace("\n  ", " ") for b in text.split("\n- ")[1:]]


def test_claude_card_scopes_run_only_verbs_to_ctx_run_output():
    """Measured: 4 of 20 sessions ran `ctx ask --intent diagnose`, `ctx q
    'fails last'` or `ctx q 'records …'` on a plain log file and got "no
    captured test run" / "bad handle" (7 wasted turns). Each of those verbs
    appears only in the bullet that says it needs `ctx run` output, and the
    card says what to do for an existing file instead -- without growing."""
    from ctx.installer import _render_claude_md

    card = _render_claude_md("ctx")
    bullets = _card_bullets(card)
    for verb in _RUN_ONLY:
        holders = [b for b in bullets if verb in b]
        assert len(holders) == 1, verb
        assert holders[0].startswith("Only for output captured by `ctx run`"), holders[0]
    existing = [b for b in bullets if b.startswith("For an existing file")]
    assert existing and "ctx search repo:<file> PATTERN" in existing[0]
    assert "Read windows" in existing[0] and "grep" in existing[0]
    assert len(card) <= 1191  # the card's size before this wording (characters)


def test_codex_card_scopes_run_only_verbs_the_same_way():
    from ctx.installer import _render_codex_file

    bullets = _card_bullets(_render_codex_file("AGENTS.md", "ctx"))
    for verb in ("fails last | in-changed", "records run:<id>#stdout"):
        holders = [b for b in bullets if verb in b]
        assert len(holders) == 1 and "Only for output captured by `ctx run`" in holders[0]
    assert any("diagnose` reads failures of a test run captured by `ctx run`" in b
               for b in bullets)
    assert any(b.startswith("For an existing file") and "ctx search repo:<file> PATTERN" in b
               for b in bullets)


def test_install_claude_preserves_user_statusline(workspace_dir):
    from ctx.installer import install_claude

    sp = workspace_dir / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}),
                  encoding="utf-8")
    install_claude(make_ws(workspace_dir))
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert data["statusLine"]["command"] == "mine"  # never clobbered
    assert "PreToolUse" in data["hooks"]             # hooks still added


def test_codex_config_selects_statusline_items(workspace_dir):
    from ctx.installer import install_codex

    install_codex(make_ws(workspace_dir))
    cfg = (workspace_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
    import tomllib

    doc = tomllib.loads(cfg)
    assert "model" in doc["tui"]["status_line"]
    assert "UsedTokens" in doc["tui"]["status_line"]


def test_install_claude_preserves_user_hooks(workspace_dir):
    from ctx.installer import install_claude

    sp = workspace_dir / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "X", "hooks": []}]}}),
        encoding="utf-8",
    )
    install_claude(make_ws(workspace_dir))
    data = json.loads(sp.read_text(encoding="utf-8"))
    matchers = [g["matcher"] for g in data["hooks"]["PreToolUse"]]
    assert "X" in matchers  # user's hook preserved
    assert any("Bash" in m for m in matchers)  # ours added


# ------------------------------------------------------------------ setup_hosts


def test_setup_hosts_configures_all_three(workspace_dir):
    from ctx.installer import SETUP_HOSTS, setup_hosts

    report = setup_hosts(make_ws(workspace_dir))
    for host in SETUP_HOSTS:
        assert f"── {host} ──" in report
    assert (workspace_dir / ".agents" / "plugins" / "ctx-harness").is_dir()
    assert (workspace_dir / ".claude" / "settings.json").is_file()
    assert (workspace_dir / ".codex" / "config.toml").is_file()
    assert (workspace_dir / "AGENTS.md").is_file()


# ------------------------------------------------------------------ Codex hooks


def test_codex_pre_tool_use_rewrites_flood(workspace_dir):
    from ctx.hook import main_pre_tool_use

    out = _run_hook(
        main_pre_tool_use,
        {
            "cwd": str(workspace_dir),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat huge.log"},
        },
        "codex",
    )
    # Rewrites are withheld on Codex (approval policy not established to
    # survive a hook allow): the original runs, the output gate bounds it.
    assert out == {}


def test_codex_pre_tool_use_allows_bounded(workspace_dir):
    from ctx.hook import main_pre_tool_use

    out = _run_hook(
        main_pre_tool_use,
        {
            "cwd": str(workspace_dir),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
        "codex",
    )
    # Codex treats a successful empty response as pass-through.  Its explicit
    # `allow` value is reserved for responses that also carry `updatedInput`.
    assert out == {}


def test_codex_post_tool_use_substitutes_flood(state_home, workspace_dir):
    from ctx.hook import main_post_tool_use

    (workspace_dir / ".ctx-session-reads").mkdir(exist_ok=True)
    out = _run_hook(
        main_post_tool_use,
        {
            "cwd": str(workspace_dir),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat big"},
            "tool_response": {"stdout": "line\n" * 8000},
        },
        "codex",
    )
    assert out.get("decision") == "block"
    assert out["reason"].lstrip().startswith("[ctx ")


def test_codex_post_tool_use_small_noop(state_home, workspace_dir):
    from ctx.hook import main_post_tool_use

    out = _run_hook(
        main_post_tool_use,
        {
            "cwd": str(workspace_dir),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_response": {"stdout": "hi"},
        },
        "codex",
    )
    assert out == {}
