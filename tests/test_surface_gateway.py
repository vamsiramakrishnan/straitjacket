"""Acceptance: Phase 4 — progressive-disclosure MCP gateway.

The gateway starts with only the compact index + reveal/hide; backend tools
appear only when their family is revealed; calls proxy to the live backend;
reveal/hide flips surface_changed (drives tools/list_changed). Fails soft."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ctx import surface_gateway as gw


_FAKE = (
    "import sys,json\n"
    "for line in sys.stdin:\n"
    " line=line.strip()\n"
    " if not line: continue\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'gh','version':'1'}}}),flush=True)\n"
    " elif meth=='tools/list': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[{'name':'search_code','description':'Search code','inputSchema':{'type':'object','properties':{'q':{'type':'string'}}}}]}}),flush=True)\n"
    " elif meth=='tools/call': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'content':[{'type':'text','text':'hits:'+str(m['params']['arguments'].get('q'))}]}}),flush=True)\n"
)


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    fake = root / "fake_mcp.py"
    fake.write_text(_FAKE, encoding="utf-8")
    (root / "ctx.toml").write_text("version=1\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": sys.executable, "args": [str(fake)]}}}), encoding="utf-8")
    return root


def test_starts_with_only_index_and_meta(ws):
    g = gw.Gateway(ws)
    names = {t["name"] for t in g.visible_tools()}
    assert names == {"surface_index", "surface_reveal", "surface_hide"}
    g.close()


def test_family_directory(ws):
    g = gw.Gateway(ws)
    assert g.families() == {"remote-source-control": ["github"]}
    g.close()


def test_reveal_surfaces_backend_tool_and_flips_changed(ws):
    g = gw.Gateway(ws)
    res, changed = g.call("surface_reveal", {"family": "remote-source-control"})
    assert changed is True
    names = {t["name"] for t in g.visible_tools()}
    assert "mcp__github__search_code" in names
    g.close()


def test_proxied_call_reaches_live_backend(ws):
    g = gw.Gateway(ws)
    g.call("surface_reveal", {"family": "remote-source-control"})
    res, _ = g.call("mcp__github__search_code", {"q": "needle"})
    assert res["content"][0]["text"] == "hits:needle"
    g.close()


def test_hidden_backend_call_is_refused(ws):
    g = gw.Gateway(ws)
    res, _ = g.call("mcp__github__search_code", {"q": "x"})
    assert "hidden" in res["content"][0]["text"]
    g.close()


def test_hide_removes_tool_and_flips_changed(ws):
    g = gw.Gateway(ws)
    g.call("surface_reveal", {"family": "remote-source-control"})
    res, changed = g.call("surface_hide", {"family": "remote-source-control"})
    assert changed is True
    assert "mcp__github__search_code" not in {t["name"] for t in g.visible_tools()}
    g.close()


def test_reveal_persists_to_state(ws):
    g = gw.Gateway(ws)
    g.call("surface_reveal", {"family": "remote-source-control"})
    g.close()
    assert "remote-source-control" in gw.load_state(ws)
    # a fresh gateway honours persisted reveal
    g2 = gw.Gateway(ws)
    assert "mcp__github__search_code" in {t["name"] for t in g2.visible_tools()}
    g2.close()


def test_unknown_family_is_rejected(ws):
    g = gw.Gateway(ws)
    res, changed = g.call("surface_reveal", {"family": "nope"})
    assert changed is False and "unknown family" in res["content"][0]["text"]
    g.close()


def test_kernel_family_cannot_be_hidden(ws):
    g = gw.Gateway(ws)
    res, changed = g.call("surface_hide", {"family": "harness"})
    assert changed is False and "kernel" in res["content"][0]["text"]
    g.close()


def test_serve_loop_over_stdio(ws):
    """Full JSON-RPC round trip through the real serve loop."""
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "surface_reveal",
                               "arguments": {"family": "remote-source-control"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}),
    ]) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "surface", "gateway", "--workspace", str(ws)],
        input=payload.encode(),
        env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
             "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True, timeout=40,
    )
    msgs = [json.loads(l) for l in proc.stdout.decode().splitlines() if l.strip()]
    # initialize reply advertises listChanged
    init = next(m for m in msgs if m.get("id") == 1)
    assert init["result"]["capabilities"]["tools"]["listChanged"] is True
    # first tools/list: only meta; second (after reveal): includes backend tool
    lists = [m for m in msgs if m.get("id") in (2, 4)]
    first = {t["name"] for t in next(m for m in lists if m["id"] == 2)["result"]["tools"]}
    second = {t["name"] for t in next(m for m in lists if m["id"] == 4)["result"]["tools"]}
    assert "mcp__github__search_code" not in first
    assert "mcp__github__search_code" in second
    # a list_changed notification was emitted after the reveal
    assert any(m.get("method") == "notifications/tools/list_changed" for m in msgs)
