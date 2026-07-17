"""Acceptance: workspace resolution/confinement, installer, doctor, MCP."""

import json
import os

import pytest

from conftest import make_store, make_ws


def test_workspace_resolution_order(tmp_path, monkeypatch):
    from ctx.workspace import resolve_workspace

    # ctx.toml ancestor wins for a plain folder.
    root = tmp_path / "proj"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(cwd=sub)
    assert ws.root == root

    # Explicit --workspace always wins.
    other = tmp_path / "other"
    other.mkdir()
    ws2 = resolve_workspace(str(other), cwd=sub)
    assert ws2.root == other


def test_git_workspace_resolution(git_workspace):
    from ctx.workspace import resolve_workspace

    sub = git_workspace / "deep"
    sub.mkdir()
    ws = resolve_workspace(cwd=sub)
    assert ws.root == git_workspace
    assert ws.git is not None and ws.git.head


def test_plain_folder_fallback(tmp_path):
    from ctx.workspace import resolve_workspace

    lonely = tmp_path / "lonely"
    lonely.mkdir()
    ws = resolve_workspace(cwd=lonely)
    assert ws.root == lonely
    assert ws.workspace_id.startswith("ws_")


def test_path_escape_rejected(workspace_dir):
    from ctx.workspace import PathEscapeError

    ws = make_ws(workspace_dir)
    with pytest.raises(PathEscapeError):
        ws.confine("../outside.txt")
    with pytest.raises(PathEscapeError):
        ws.confine("/etc/passwd")


def test_symlink_escape_rejected(tmp_path, workspace_dir):
    from ctx.workspace import PathEscapeError

    ws = make_ws(workspace_dir)
    outside = tmp_path / "target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace_dir / "innocent.txt"
    os.symlink(outside, link)
    with pytest.raises(PathEscapeError):
        ws.confine("innocent.txt")


def test_stable_workspace_id_with_repo_key(tmp_path):
    from ctx.workspace import resolve_workspace

    a, b = tmp_path / "clone-a", tmp_path / "clone-b"
    for d in (a, b):
        d.mkdir()
        (d / "ctx.toml").write_text('version = 1\nrepo_key = "proj-x"\n', encoding="utf-8")
    assert resolve_workspace(str(a)).workspace_id == resolve_workspace(str(b)).workspace_id


def test_monorepo_scope_narrowing(state_home, tmp_path):
    from ctx.retrieval import search
    from ctx.workspace import resolve_workspace

    root = tmp_path / "mono"
    (root / "services" / "payments").mkdir(parents=True)
    (root / "apps" / "web").mkdir(parents=True)
    (root / "ctx.toml").write_text(
        'version = 1\n[scopes.payments]\nroots = ["services/payments"]\n', encoding="utf-8"
    )
    (root / "services" / "payments" / "s.py").write_text("needle = 1\n", encoding="utf-8")
    (root / "apps" / "web" / "w.py").write_text("needle = 2\n", encoding="utf-8")
    ws = resolve_workspace(str(root))
    store = make_store(ws)
    out = search(store, ws, "repo:", ["needle"], scope="payments")
    assert "services/payments/s.py" in out
    assert "apps/web/w.py" not in out


def test_installer_renders_valid_plugin(workspace_dir):
    from ctx.installer import render_plugin

    dest = render_plugin(workspace_dir, ctx_exe="/opt/bin/ctx")
    for name in ("plugin.json", "hooks.json", "mcp_config.json"):
        data = json.loads((dest / name).read_text(encoding="utf-8"))
        assert data
    hooks = json.loads((dest / "hooks.json").read_text(encoding="utf-8"))
    cmd = hooks["ctx-harness"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd.startswith("/opt/bin/ctx")  # absolute path, CWD-independent
    assert (dest / "skills" / "ctx-harness" / "SKILL.md").is_file()


def test_installer_refuses_duplicate_installation(workspace_dir):
    from ctx.installer import render_plugin

    standalone = workspace_dir / ".agents" / "skills" / "ctx-harness"
    standalone.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="standalone skill"):
        render_plugin(workspace_dir, ctx_exe="/opt/bin/ctx")


def test_doctor_flags_duplicate_installation(state_home, workspace_dir):
    from ctx.installer import doctor_report, render_plugin

    ws = make_ws(workspace_dir)
    render_plugin(workspace_dir, ctx_exe="/opt/bin/ctx")
    (workspace_dir / ".agents" / "skills" / "ctx-harness").mkdir(parents=True)
    report = doctor_report(ws, antigravity=True)
    assert "PROBLEMS FOUND" in report
    assert "duplicate" in report


def test_doctor_healthy_after_install(state_home, workspace_dir):
    from ctx.installer import doctor_report, init_workspace, render_plugin

    ws = make_ws(workspace_dir)
    init_workspace(workspace_dir, quiet=True)
    render_plugin(workspace_dir, ctx_exe="/opt/bin/ctx")
    ws = make_ws(workspace_dir)  # reload policy
    report = doctor_report(ws, antigravity=True)
    assert "valid JSON" in report
    assert "✓ hook classifier" in report


def test_mcp_end_to_end(state_home, workspace_dir):
    """Drive the real stdio server: initialize → tools/list → tools/call."""
    import io
    import subprocess
    import sys
    from pathlib import Path

    (workspace_dir / "code.py").write_text("magic_token = 42\n", encoding="utf-8")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "ctx",
                "arguments": {
                    "op": "search",
                    "workspace": str(workspace_dir),
                    "ref": "repo:",
                    "patterns": ["magic_token"],
                },
            },
        },
    ]
    payload = "\n".join(json.dumps(m) for m in msgs) + "\n"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "mcp", "--bounded-only"],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    replies = [json.loads(ln) for ln in proc.stdout.decode().splitlines() if ln.strip()]
    by_id = {r.get("id"): r for r in replies}
    assert by_id[1]["result"]["serverInfo"]["name"] == "ctx-harness"
    tools = by_id[2]["result"]["tools"]
    assert len(tools) == 1 and tools[0]["name"] == "ctx"
    call = by_id[3]["result"]
    assert call["isError"] is False
    text = call["content"][0]["text"]
    assert "magic_token" in text and "code.py" in text


def test_cli_run_and_search_roundtrip(state_home, workspace_dir, capsys):
    import sys

    from ctx.cli import main

    rc = main(
        [
            "--workspace",
            str(workspace_dir),
            "run",
            "--",
            sys.executable,
            "-c",
            "print('ERROR: kaboom occurred')",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("[ctx run:")
    assert "kaboom" in out
    run_id = out.split("run:", 1)[1].split()[0]

    rc = main(["--workspace", str(workspace_dir), "search", f"run:{run_id}", "kaboom"])
    out2 = capsys.readouterr().out
    assert rc == 0
    assert "kaboom" in out2 and "matches: 1" in out2
