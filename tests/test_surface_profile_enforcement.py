"""A profile's per-tool restrictions are enforced at launch — or the server is
left out of the session and the launch says so. Every assertion here is on
what the launched session could actually CALL (driving the generated
gateway, or the server it would reach directly), never on the plan's own
description of itself."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ctx import surface
from ctx import surface_gateway as gw
from ctx import surface_launch as sl

_FAKE = (
    "import sys,json\n"
    "open(sys.argv[1],'a').write('started\\n')\n"
    "for line in sys.stdin:\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{}}}),flush=True)\n"
    " elif meth=='tools/list': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[\n"
    "  {'name':'read_file','description':'Read a file','inputSchema':{'type':'object'}},\n"
    "  {'name':'delete_file','description':'Delete a file permanently','inputSchema':{'type':'object'}}]}}),flush=True)\n"
    " elif meth=='tools/call': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'content':[{'type':'text','text':'CALLED '+m['params']['name']}]}}),flush=True)\n"
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for v in ("CLAUDE_CONFIG_DIR", "ENABLE_TOOL_SEARCH", "ANTHROPIC_BASE_URL", "CODEX_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(surface, "MANAGED_MCP_PATHS", (str(tmp_path / "none.json"),))


def _ws(tmp_path: Path, toml: str, *, warm: bool, remote: bool = False) -> tuple[Path, Path]:
    ws = tmp_path / "proj"
    ws.mkdir()
    marker = tmp_path / "spawns"
    (ws / "fake.py").write_text(_FAKE)
    servers = {"files": {"command": sys.executable, "args": [str(ws / "fake.py"), str(marker)]}}
    if remote:
        servers["web"] = {"type": "http", "url": "https://web.example/mcp"}
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.local.json").write_text('{"enableAllProjectMcpServers": true}')
    (ws / "ctx.toml").write_text("version = 1\n" + toml)
    if warm:
        surface.audit(ws, probe_mcp=True)
        marker.unlink()
    return ws, marker


def _launch(ws: Path, tmp_path: Path, profile: str) -> tuple[sl.LaunchPlan, Path]:
    tmp = tmp_path / "launch"
    tmp.mkdir()
    plan = sl.plan_claude_launch(ws, [], mode=profile, child_env={},
                                 parent_env=dict(os.environ), tmpdir=tmp)
    return plan, tmp


def _callable(ws: Path, tmp: Path) -> dict[str, str]:
    """What the session could call: tool -> result of calling it. Through
    the gateway everything revealable is revealed first; a server reached
    directly is asked for its own catalogue (all of it is callable)."""
    out: dict[str, str] = {}
    mcp = json.loads((tmp / "mcp.json").read_text())["mcpServers"]
    for name, cfg in mcp.items():
        if name == sl.GATEWAY_NAME:
            g = gw.Gateway(ws, backends_file=tmp / "backends.json", persist=False)
            for server in g.commands:
                g.call("surface_reveal", {"target": server})
            for want in ("read_file", "delete_file"):
                for server in g.commands:
                    res = g.call(f"mcp__{server}__{want}", {})[0]["content"][0]["text"]
                    if res.startswith("CALLED"):
                        out[f"{server}.{want}"] = res
            listed = {t["name"] for t in g.visible_tools()}
            out["_listed"] = ",".join(sorted(listed))
            g.close()
        elif "command" in cfg:
            for t in surface.probe_mcp_tools([cfg["command"], *cfg["args"]]):
                out[f"{name}.{t['name']}"] = "direct"
        else:
            out[f"{name}.*"] = "remote, every tool"
    return out


_NODELETE = '[surface.profiles.nodelete]\nexclude = ["mcp.files.delete_file"]\n'


@pytest.mark.parametrize("gateway", [False, True])
def test_excluded_tool_is_not_callable_with_warm_cache(tmp_path, gateway):
    ws, _ = _ws(tmp_path, f"[surface]\ngateway = {str(gateway).lower()}\n" + _NODELETE, warm=True)
    plan, tmp = _launch(ws, tmp_path, "nodelete")
    reach = _callable(ws, tmp)
    assert "files.read_file" in reach
    assert "files.delete_file" not in reach and "delete_file" not in reach["_listed"]
    backend = json.loads((tmp / "backends.json").read_text())["mcpServers"]["files"]
    assert backend["allow_tools"] == ["read_file"]
    assert plan.receipt["profile_enforcement"]["per_tool_enforced_by_gateway"] == ["files"]


def test_include_list_naming_one_tool(tmp_path):
    toml = ('[surface.profiles.onlyread]\nfamilies = ["docs"]\n'
            'include = ["mcp.files.read_file"]\n')
    ws, _ = _ws(tmp_path, toml, warm=True)
    plan, tmp = _launch(ws, tmp_path, "onlyread")
    reach = _callable(ws, tmp)
    assert "files.read_file" in reach and "files.delete_file" not in reach


def test_authority_ceiling_filters_tools_of_kept_server(tmp_path):
    ws, _ = _ws(tmp_path, _READCEILING, warm=True)
    plan, tmp = _launch(ws, tmp_path, "readceiling")
    reach = _callable(ws, tmp)
    assert "files.read_file" in reach and "files.delete_file" not in reach


_READCEILING = '[surface.profiles.readceiling]\nauthority_ceiling = "read"\n'


@pytest.mark.parametrize("profile,toml", [("nodelete", _NODELETE),
                                          ("readceiling", _READCEILING)])
def test_cold_catalogue_is_refused_unspawned_and_said(tmp_path, profile, toml):
    ws, marker = _ws(tmp_path, toml, warm=False)
    plan, tmp = _launch(ws, tmp_path, profile)
    assert not marker.exists()                                  # nothing was started
    reach = _callable(ws, tmp)
    assert not any(k.startswith("files.") for k in reach)        # server not reachable
    dropped = plan.receipt["profile_enforcement"]["dropped_unenforceable"]
    assert [d["server"] for d in dropped] == ["files"]
    assert "catalogue is unknown" in dropped[0]["reason"]
    assert "--probe-mcp" in plan.receipt["profile_enforcement"]["remedy"]
    assert "DROPPED files" in plan.line
    assert "dropped because a per-tool restriction could not be enforced" in plan.receipt["profile"]


def test_remote_server_with_per_tool_exclusion_is_refused(tmp_path):
    toml = '[surface.profiles.noremotedelete]\nexclude = ["mcp.web.delete_page"]\n'
    ws, _ = _ws(tmp_path, toml, warm=True, remote=True)
    plan, tmp = _launch(ws, tmp_path, "noremotedelete")
    reach = _callable(ws, tmp)
    assert "web.*" not in reach
    assert "files.delete_file" in reach            # files is not restricted by this profile
    dropped = plan.receipt["profile_enforcement"]["dropped_unenforceable"]
    assert [d["server"] for d in dropped] == ["web"] and "not proxied" in dropped[0]["reason"]


def test_unrestricted_profile_passes_servers_through(tmp_path):
    ws, _ = _ws(tmp_path, "", warm=True, remote=True)
    plan, tmp = _launch(ws, tmp_path, "full")
    reach = _callable(ws, tmp)
    assert {"files.read_file", "files.delete_file", "web.*"} <= set(reach)
    assert plan.receipt["profile_enforcement"]["dropped_unenforceable"] == []
