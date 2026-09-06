"""Native config contracts and real MCP subprocess round trips (no provider)."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_ws
from ctx import mcp_hosts as mh


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    """Exercise Hermes' documented config CLI without credentials or a model."""
    binary = tmp_path / "hermes"
    config = tmp_path / "profile.json"
    config.write_text(json.dumps({"model": "my-choice", "mcp_servers": {"other": {"url": "https://example.invalid"}}}))
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
p = Path(os.environ["TEST_HERMES_CONFIG"])
d = json.loads(p.read_text())
args = sys.argv[1:]
if args == ["config", "get", "mcp_servers", "--json"]:
    print(json.dumps(d.get("mcp_servers", {})))
elif args[:3] == ["config", "set", "mcp_servers.ctx-harness"]:
    d.setdefault("mcp_servers", {})["ctx-harness"] = json.loads(args[3])
    p.write_text(json.dumps(d))
elif args == ["config", "path"]:
    print(p)
elif args == ["plugins", "enable", "straitjacket"]:
    d.setdefault("plugins", {})["enabled"] = ["straitjacket"]
    p.write_text(json.dumps(d))
elif args[:2] == ["config", "get"] and args[2].startswith("plugins."):
    print(json.dumps(d.get("plugins", {}).get(args[2].split(".")[1], [])))
else:
    sys.exit(9)
''')
    binary.chmod(0o755)
    monkeypatch.setenv("TEST_HERMES_CONFIG", str(config))
    real_which = mh.shutil.which
    monkeypatch.setattr(mh.shutil, "which", lambda name: str(binary) if name == "hermes" else real_which(name))
    return config


@pytest.mark.parametrize("host", mh.HOSTS)
def test_config_launches_real_mcp_in_selected_workspace(host, tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "evidence.txt").write_text("workspace evidence\n")
    exe = shlex.join([sys.executable, "-m", "ctx"])
    cfg = mh.configuration(host, root, exe)
    if host == "opencode":
        argv = cfg["mcp"][mh.SERVER]["command"]
    else:
        server = (cfg[0]["insert"][0]["config"] if host == "dsh" else
                  cfg["mcp_servers" if host == "hermes" else "mcpServers"][mh.SERVER])
        argv = [server["command"], *server["args"]]
    argv = [str(root) if arg == "${workspaceFolder}" else arg for arg in argv]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": host, "version": "test"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ctx", "arguments": {"op": "search", "ref": "repo:", "patterns": ["workspace evidence"]}}},
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "CTX_STATE_HOME": str(tmp_path / "state")}
    result = subprocess.run(argv, input="\n".join(map(json.dumps, requests)) + "\n", capture_output=True, text=True, cwd=tmp_path, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[1]["result"]["tools"][0]["name"] == "ctx"
    assert not replies[2]["result"].get("isError"), replies[2]
    assert "evidence.txt" in json.dumps(replies[2])


@pytest.mark.parametrize("host,key", [("omp", "mcpServers"), ("opencode", "mcp")])
def test_json_merge_preserves_settings_and_is_idempotent(host, key, workspace_dir):
    path = workspace_dir / mh.FILES[host]
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"model": "my-choice", key: {"other": {"url": "https://example.invalid"}}}))
    mh.install(host, make_ws(workspace_dir))
    after = path.read_bytes()
    mh.install(host, make_ws(workspace_dir))
    assert path.read_bytes() == after
    data = json.loads(after)
    assert data["model"] == "my-choice"
    assert data[key]["other"] == {"url": "https://example.invalid"}
    assert all(ok for _, ok, _ in mh.checks(workspace_dir))
    data[key][mh.SERVER]["enabled"] = False
    path.write_text(json.dumps(data))
    assert not mh.checks(workspace_dir)[0][1]
    assert mh.conflicts(host, workspace_dir)


@pytest.mark.parametrize("host", ["omp", "opencode"])
@pytest.mark.parametrize("content", ["{broken", "[]", '{"mcp": [], "mcpServers": []}'])
def test_bad_config_refuses_without_writes(host, content, workspace_dir):
    policy = (workspace_dir / "ctx.toml").read_bytes()
    path = workspace_dir / mh.FILES[host]
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    with pytest.raises(mh.IntegrationError):
        mh.install(host, make_ws(workspace_dir))
    assert path.read_text() == content
    assert (workspace_dir / "ctx.toml").read_bytes() == policy


def test_jsonc_is_never_shadowed(workspace_dir):
    path = workspace_dir / "opencode.jsonc"
    path.write_text('{ // keep my comment\n "model": "my-choice"\n}')
    with pytest.raises(mh.IntegrationError, match="JSONC"):
        mh.install("opencode", make_ws(workspace_dir))
    assert not (workspace_dir / "opencode.json").exists()
    assert "keep my comment" in path.read_text()


def test_hermes_native_writer_preserves_profile(fake_hermes, workspace_dir):
    mh.install("hermes", make_ws(workspace_dir))
    after = fake_hermes.read_bytes()
    mh.install("hermes", make_ws(workspace_dir))
    assert fake_hermes.read_bytes() == after
    data = json.loads(after)
    assert data["model"] == "my-choice"
    assert "other" in data["mcp_servers"]
    assert "${workspaceFolder}" in data["mcp_servers"][mh.SERVER]["args"]
    assert mh.checks(workspace_dir)[0][1]
    data["mcp_servers"][mh.SERVER]["enabled"] = False
    fake_hermes.write_text(json.dumps(data))
    assert not mh.checks(workspace_dir)[0][1]
    with pytest.raises(mh.IntegrationError, match="already defines"):
        mh.install("hermes", make_ws(workspace_dir))


def test_hermes_cli_failure_is_not_ready(workspace_dir, monkeypatch):
    monkeypatch.setattr(mh.shutil, "which", lambda name: None)
    mh.install("hermes", make_ws(workspace_dir))
    assert not mh.checks(workspace_dir)[0][1]
    assert mh.wrap("hermes", workspace_dir) != 0


@pytest.mark.parametrize("host", mh.HOSTS)
def test_launch_preserves_arguments_exit_status_and_dsh_overlay(host, workspace_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(mh, "install", lambda *a, **kw: "prepared")
    monkeypatch.setattr(mh.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(mh.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 7))
    args = ["--profile", "headless", "a task with spaces"] if host == "dsh" else ["--model", "user-choice", "prompt with spaces"]
    assert mh.wrap(host, workspace_dir, args) == 7
    argv, kw = calls[0]
    assert argv[-len(args):] == args
    assert kw["cwd"] == workspace_dir
    assert "shell" not in kw
    if host == "dsh":
        assert argv[1:3] == ["--patch", str(workspace_dir / mh.FILES[host])]


@pytest.mark.parametrize("host", mh.HOSTS)
def test_registry_discloses_limits_and_resolves_dispatch(host):
    from ctx.hosts import host_by_name, installer_for, wrapper_for
    spec = host_by_name(host)
    assert spec.harnessable and spec.supports_mcp
    assert spec.output_substitution and spec.supports_hooks
    assert spec.input_substitution == (host != "dsh")
    assert not spec.unattended  # becomes eligible only with explicit ACP configuration
    assert spec.default_model == "unknown"
    assert callable(installer_for(spec)) and callable(wrapper_for(spec))


def test_aliases_and_setup_fingerprint(workspace_dir):
    from ctx.hosts import host_by_name
    from ctx.setup_telemetry import setup_fingerprint
    assert host_by_name("open-hermes").name == "hermes"
    assert host_by_name("oh-my-pi").name == "omp"
    before = setup_fingerprint(workspace_dir)
    mh.install("dsh", make_ws(workspace_dir))
    assert setup_fingerprint(workspace_dir) != before


def test_symlinked_config_directory_is_refused(workspace_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace_dir / ".omp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(mh.IntegrationError, match="symlink"):
        mh.install("omp", make_ws(workspace_dir))
    assert not (outside / "mcp.json").exists()


def test_hermes_launch_profile_mismatch_refuses_before_writing(workspace_dir, monkeypatch):
    monkeypatch.setattr(mh, "install", lambda *a, **kw: pytest.fail("must not install into another profile"))
    assert mh.wrap("hermes", workspace_dir, ["--profile", "other", "chat"]) == 2


def test_print_config_uses_explicit_workspace_and_alias(workspace_dir, capsys):
    from ctx.cli import main
    assert main(["--workspace", str(workspace_dir), "wrap", "oh-my-pi", "--print-config"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mcpServers"][mh.SERVER]["args"][-1] == str(workspace_dir)
    assert not (workspace_dir / ".omp").exists()


def test_no_orchestration_launch_for_mcp_only_host(workspace_dir, monkeypatch):
    from ctx.hosts import detect, host_by_name
    from ctx.orchestrator import _launch_host
    host = detect(host_by_name("dsh"), which=lambda _: "/bin/dsh", env={})
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("no worker transport"))
    code, out, err, usage = _launch_host(host, workspace_dir, "task", "ctx", timeout=1)
    assert code == 2 and not out and usage is None
    assert "no orchestration transport" in err
