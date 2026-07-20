"""Acceptance: Phase 3 — compile a minimal capability surface from a profile
and emit enforceable per-host config. Selection, checks, and per-host emit."""

import json
from pathlib import Path

import pytest

from ctx import surface, surface_profiles as sp


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp", "args": ["serve"]},
        "jira": {"command": "jira-mcp"},
        "ctx-harness": {"command": "ctx", "args": ["mcp", "--bounded-only"]},
    }}), encoding="utf-8")
    (root / ".claude" / "skills" / "reader").mkdir(parents=True)
    (root / ".claude" / "skills" / "reader" / "SKILL.md").write_text(
        "# Reader\nRead and search files locally.\n", encoding="utf-8")
    return root


def test_builtin_profiles_load():
    assert sp.load_profile("read-only", "/nonexistent").authority_ceiling == "read"
    assert sp.load_profile("local-dev", "/nonexistent") is not None
    assert sp.load_profile("nope", "/nonexistent") is None


def test_ctx_toml_profile_overrides(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        "version=1\n[surface.profiles.custom]\n"
        "families = ['repository']\nauthority_ceiling = 'read'\n",
        encoding="utf-8")
    p = sp.load_profile("custom", tmp_path)
    assert p is not None and p.families == frozenset({"repository"})


def test_read_only_drops_remote_and_collab_servers(ws):
    rep = sp.compile_profile(ws, "read-only", host="claude")
    assert "github" in rep["servers_dropped"]
    assert "jira" in rep["servers_dropped"]
    assert "ctx-harness" in rep["servers_kept"]      # kernel kept
    assert rep["tokens"]["after_gateway"] <= rep["tokens"]["after_server"] <= rep["tokens"]["before"]


def test_full_profile_keeps_everything(ws):
    rep = sp.compile_profile(ws, "full", host="claude")
    assert rep["servers_dropped"] == []
    assert set(rep["servers_kept"]) >= {"github", "jira", "ctx-harness"}


def test_kernel_always_kept_even_under_read_only(ws):
    rep = sp.compile_profile(ws, "read-only", host="claude")
    assert any(s == "ctx-harness" for s in rep["servers_kept"])


def test_emit_claude_is_valid_and_strict(ws):
    rep = sp.compile_profile(ws, "read-only", host="claude", apply=True)
    mcp = json.loads((ws / sp.COMPILE_DIR / "mcp.claude.json").read_text())
    assert set(mcp["mcpServers"]) == {"ctx-harness"}      # only selected
    settings = json.loads((ws / sp.COMPILE_DIR / "settings.claude.json").read_text())
    deny = settings["permissions"]["deny"]
    assert "mcp__github__*" in deny and "mcp__jira__*" in deny
    assert "--strict-mcp-config" in rep["launch"]


def test_emit_codex_is_valid_toml(ws):
    import tomllib

    rep = sp.compile_profile(ws, "read-only", host="codex", apply=True)
    doc = tomllib.loads((ws / sp.COMPILE_DIR / "config.codex.toml").read_text())
    assert set(doc["mcp_servers"]) == {"ctx-harness"}
    assert "github" not in doc["mcp_servers"]


def test_emit_antigravity_is_valid_json(ws):
    rep = sp.compile_profile(ws, "read-only", host="antigravity", apply=True)
    doc = json.loads((ws / sp.COMPILE_DIR / "mcp_config.antigravity.json").read_text())
    assert set(doc["mcpServers"]) == {"ctx-harness"}


def test_dependency_closure_check_fires():
    # a repository skill kept by the profile but requiring a dropped server
    skill = surface.Capability(
        id="skill.finder", kind="skill", provider="repo", source="s", tokens=100,
        authority="n/a", family="repository", requires=("mcp__github__search_code",))
    ctx_server = surface.Capability(
        id="mcp.ctx-harness", kind="mcp_server", provider="ctx-harness",
        source="x", tokens=10, family="harness")
    issues = sp.check([skill, ctx_server], sp.BUILTIN_PROFILES["read-only"])
    assert any("github" in i for i in issues)


_MIXED_SERVER = (
    "import sys,json\n"
    "for line in sys.stdin:\n"
    " line=line.strip()\n"
    " if not line: continue\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'fs','version':'1'}}}),flush=True)\n"
    " elif meth=='tools/list': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[\n"
    "   {'name':'read_file','description':'Read a file from disk and return its text contents for inspection','inputSchema':{'type':'object','properties':{'path':{'type':'string','description':'the file path to read'}}}},\n"
    "   {'name':'delete_file','description':'Delete and permanently remove a file from the filesystem','inputSchema':{'type':'object','properties':{'path':{'type':'string','description':'the file path to delete'}}}}]}}),flush=True)\n"
)


def test_two_number_honesty_when_tools_deferred_within_kept_server(tmp_path):
    # A kept server with a read tool (needed) + a destructive tool (deferred):
    # dropping the whole server is impossible, so config saves little while the
    # gateway's per-tool control recovers the destructive tool's tokens.
    import sys

    root = tmp_path / "proj"
    root.mkdir()
    fake = root / "fs_mcp.py"
    fake.write_text(_MIXED_SERVER, encoding="utf-8")
    (root / "ctx.toml").write_text("version=1\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "fs": {"command": sys.executable, "args": [str(fake)]}}}), encoding="utf-8")

    rep = sp.compile_profile(root, "read-only", host="claude", probe_mcp=True)
    assert "fs" in rep["servers_kept"]                     # kept for read_file
    assert rep["deferred_tools"].get("fs") == ["delete_file"]
    # gateway recovers what dropping the whole server cannot
    assert rep["tokens"]["after_gateway"] < rep["tokens"]["after_server"]


def test_unknown_profile_and_host_error(ws):
    assert "error" in sp.compile_profile(ws, "bogus", host="claude")
    assert "error" in sp.compile_profile(ws, "read-only", host="bogus")


def test_authority_ceiling_gates_action_kinds():
    # family passes (repository ∈ local-dev) but destructive authority > ceiling
    tool = surface.Capability(
        id="mcp.fs.delete", kind="mcp_tool", provider="fs", source="mcp:fs",
        tokens=200, authority="destructive", family="repository")
    sel, exc = sp.select([tool], sp.BUILTIN_PROFILES["local-dev"])
    assert sel == [] and exc and "authority" in exc[0][1]
