from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def setup_ws(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    from ctx.workspace import resolve_workspace

    return resolve_workspace(str(root))


def _detected(name: str = "codex"):
    return SimpleNamespace(name=name, path=f"/bin/{name}")


def test_repeat_setup_uses_receipt_noop(setup_ws, monkeypatch, capsys):
    import ctx.installer as installer
    import ctx.wrap as wrap

    calls: list[list[str]] = []
    monkeypatch.setattr(wrap, "_guided_survey", lambda _ws: ([_detected()], [], []))
    monkeypatch.setattr(
        installer,
        "setup_hosts",
        lambda _ws, hosts: calls.append(list(hosts)) or "configured codex\n",
    )
    monkeypatch.setattr(
        installer,
        "doctor_checks",
        lambda _ws: [("ctx on PATH", True, ""), ("store writable", True, "")],
    )

    assert wrap.guided_setup(setup_ws) == 0
    first = capsys.readouterr().out
    assert wrap.guided_setup(setup_ws) == 0
    second = capsys.readouterr().out

    assert calls == [["codex"]]
    assert "[1/4]" in first
    assert "ctx setup — already ready" in second
    assert "managed config unchanged" in second
    assert len(second.encode()) < len(first.encode()) * 0.35


def test_managed_drift_repairs_and_reverifies(setup_ws, monkeypatch, capsys):
    import ctx.installer as installer
    import ctx.wrap as wrap
    from ctx.setup_telemetry import load_setup_receipt

    calls = 0

    def install(_ws, _hosts):
        nonlocal calls
        calls += 1
        return "configured codex\n"

    monkeypatch.setattr(wrap, "_guided_survey", lambda _ws: ([_detected()], [], []))
    monkeypatch.setattr(installer, "setup_hosts", install)
    monkeypatch.setattr(installer, "doctor_checks", lambda _ws: [("ready", True, "")])

    assert wrap.guided_setup(setup_ws) == 0
    capsys.readouterr()
    managed = setup_ws.root / ".ctxignore"
    managed.write_text("generated drift\n", encoding="utf-8")

    assert wrap.guided_setup(setup_ws) == 0
    capsys.readouterr()
    assert calls == 2
    assert load_setup_receipt(setup_ws.root)["strategy"] == "repair_managed"


def test_failed_setup_never_becomes_ready_noop(setup_ws, monkeypatch, capsys):
    import ctx.installer as installer
    import ctx.wrap as wrap

    calls = 0

    def install(_ws, _hosts):
        nonlocal calls
        calls += 1
        return "attempted codex\n"

    monkeypatch.setattr(wrap, "_guided_survey", lambda _ws: ([_detected()], [], []))
    monkeypatch.setattr(installer, "setup_hosts", install)
    monkeypatch.setattr(
        installer, "doctor_checks", lambda _ws: [("ctx on PATH", False, "not found")]
    )

    assert wrap.guided_setup(setup_ws) == 1
    capsys.readouterr()
    assert wrap.guided_setup(setup_ws) == 1
    assert "already ready" not in capsys.readouterr().out
    assert calls == 2


def test_setup_receipt_never_persists_config_contents(setup_ws):
    from ctx.setup_telemetry import record_setup

    config = setup_ws.root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('token = "super-secret-value"\n', encoding="utf-8")
    record_setup(
        setup_ws.root,
        ["codex"],
        strategy="configure_detected",
        success=True,
        checks_total=1,
        checks_passed=1,
        duration_ms=12.5,
    )
    raw = (setup_ws.root / ".ctx-session-reads" / "setup.json").read_text()
    assert "super-secret-value" not in raw
    assert json.loads(raw)["manual_config_edits"] == 0


def test_user_owned_codex_config_refuses_before_any_managed_write(
    setup_ws, monkeypatch, capsys
):
    import ctx.installer as installer
    import ctx.wrap as wrap
    from ctx.setup_telemetry import load_setup_receipt

    config = setup_ws.root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = '[model_providers.user]\nname = "keep-me"\n'
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(wrap, "_guided_survey", lambda _ws: ([_detected()], [], []))

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("setup_hosts must not run before the reviewed edit")

    monkeypatch.setattr(installer, "setup_hosts", unexpected_write)

    assert wrap.guided_setup(setup_ws) == 1
    output = capsys.readouterr().out
    assert "one reviewed edit needed" in output
    assert "[mcp_servers.ctx-harness]" in output
    assert "[features]" in output
    assert "hooks = true" in output
    assert "[tui]" not in output
    assert "managed files were not changed" in output
    assert config.read_text(encoding="utf-8") == original
    assert not (setup_ws.root / ".codex" / "hooks.json").exists()
    assert not (setup_ws.root / "AGENTS.md").exists()
    receipt = load_setup_receipt(setup_ws.root)
    assert receipt["success"] is False
    assert receipt["strategy"] == "refuse_unmanaged"


def test_wrap_codex_propagates_user_owned_config_refusal(setup_ws, capsys):
    import ctx.wrap as wrap

    config = setup_ws.root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.ctx-harness]\ncommand = "stale-ctx"\nargs = []\n',
        encoding="utf-8",
    )

    assert wrap.wrap_codex(setup_ws.root) == 1
    captured = capsys.readouterr()
    assert "one reviewed edit needed" in captured.out
    assert "Update the existing [mcp_servers.ctx-harness] table" in captured.out
    assert "Add this reviewed entry" not in captured.out
    assert "now harnessed" not in captured.out + captured.err


def test_wrap_claude_propagates_malformed_settings_refusal(
    setup_ws, monkeypatch, capsys
):
    import sys

    import ctx.installer as installer
    from ctx.commands.hosts import cmd_wrap

    settings = setup_ws.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not-json", encoding="utf-8")
    real_which = installer.shutil.which
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: sys.executable if name == "ctx" else real_which(name),
    )
    ns = SimpleNamespace(
        host="claude", agent_args=[], print_config=False,
        workspace=str(setup_ws.root),
    )

    assert cmd_wrap(ns) == 1
    captured = capsys.readouterr()
    assert "claude hooks" in captured.out
    assert "now harnessed" not in captured.out + captured.err


def test_ctx_managed_codex_config_with_removed_mcp_is_repaired(setup_ws):
    from ctx.installer import install_codex

    config = setup_ws.root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "# ctx-harness — straitjacket context containment for Codex CLI.\n"
        "[features]\nhooks = true\n",
        encoding="utf-8",
    )

    report = install_codex(setup_ws, init_policy=False)

    assert "refreshed managed .codex/config.toml" in report
    assert "[mcp_servers.ctx-harness]" in config.read_text(encoding="utf-8")


def test_doctor_rejects_broken_codex_mcp_launch_contract(setup_ws):
    from ctx.installer import doctor_checks

    config = setup_ws.root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.ctx-harness]\ncommand = "/python -m ctx"\nargs = []\n',
        encoding="utf-8",
    )

    rows = {name: (ok, detail) for name, ok, detail in doctor_checks(setup_ws)}
    assert rows["codex MCP"][0] is False
    assert "stale or contains arguments" in rows["codex MCP"][1]


def test_doctor_rejects_codex_config_with_hooks_feature_disabled(setup_ws):
    from ctx.installer import doctor_checks, install_codex

    install_codex(setup_ws, init_policy=False)
    config = setup_ws.root / ".codex" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("hooks = true", "hooks = false"),
        encoding="utf-8",
    )

    rows = {name: (ok, detail) for name, ok, detail in doctor_checks(setup_ws)}
    assert rows["codex MCP"][0] is False
    assert "hooks = true" in rows["codex MCP"][1]


def test_setup_fingerprint_tracks_the_actual_ctx_executable(setup_ws, monkeypatch):
    import ctx.setup_telemetry as telemetry

    first = setup_ws.root / "bin-a" / "ctx"
    second = setup_ws.root / "bin-b" / "ctx"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    monkeypatch.setattr(telemetry.shutil, "which", lambda _name: str(first))
    before = telemetry.setup_fingerprint(setup_ws.root)
    monkeypatch.setattr(telemetry.shutil, "which", lambda _name: str(second))
    after = telemetry.setup_fingerprint(setup_ws.root)

    assert before != after


def test_setup_fingerprint_tracks_nested_plugin_and_claude_explorer(setup_ws):
    import ctx.setup_telemetry as telemetry

    before = telemetry.setup_fingerprint(setup_ws.root)
    nested = setup_ws.root / ".agents" / "plugins" / "ctx-harness" / "skills" / "card.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("one", encoding="utf-8")
    after_plugin = telemetry.setup_fingerprint(setup_ws.root)

    explorer = setup_ws.root / ".claude" / "agents" / "ctx-explorer.md"
    explorer.parent.mkdir(parents=True)
    explorer.write_text("two", encoding="utf-8")
    after_explorer = telemetry.setup_fingerprint(setup_ws.root)

    assert before != after_plugin
    assert after_plugin != after_explorer


def test_setup_parser_exposes_short_front_door():
    from ctx.cli import _build_parser

    ns = _build_parser().parse_args(
        ["setup", "--host", "codex", "--host", "claude", "--repair"]
    )
    assert ns.cmd == "setup"
    assert ns.hosts == ["codex", "claude"]
    assert ns.repair is True
