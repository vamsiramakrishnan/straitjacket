"""Acceptance: the launch-time surface plan (`ctx wrap claude --surface`).

Discovery reaches user/local scope read-only; pricing is honest (a skill
costs its listing entry, an MCP server costs names+instructions when the host
defers); the gateway keeps backend env/cwd without printing values and
discloses by query or by server/tool; the wrapper applies the plan only
through ephemeral channels, the caller always wins, and nothing but the
documented receipt is left behind. No real agent is launched: a fake
`claude` script records what it was given."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from ctx import surface
from ctx import surface_gateway as gw
from ctx import surface_launch as sl

_ENV_VARS = ("CLAUDE_CONFIG_DIR", "ENABLE_TOOL_SEARCH", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
             "ANTHROPIC_BASE_URL", "SLASH_COMMAND_TOOL_CHAR_BUDGET", "CODEX_HOME",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CTX_WRAP_NO_TOOL_DIET",
             "CTX_WRAP_NO_DISCIPLINE")
SECRET = "s3cret-value-do-not-print"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for v in _ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    # No enterprise config on the test host may leak into a plan.
    monkeypatch.setattr(surface, "MANAGED_MCP_PATHS", (str(tmp_path / "no-managed.json"),))


def _home() -> Path:
    return Path(os.environ["HOME"])


def _tree(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _skill(root: Path, name: str, desc: str, *, body: str = "", extra: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(f"---\nname: {name}\ndescription: {desc}\n{extra}---\n# {name}\n"
                 + (body or "Step. " * 300), encoding="utf-8")
    return p


def _approve(ws: Path) -> None:
    """Approve every project (.mcp.json) server, as a user who accepted the
    trust prompt would; without it Claude Code starts none of them."""
    (ws / ".claude").mkdir(exist_ok=True)
    (ws / ".claude" / "settings.local.json").write_text(
        json.dumps({"enableAllProjectMcpServers": True}))


def _transcript(ws: Path, events: list[dict], *, config_dir: Path | None = None,
                sessions: int = 5) -> Path:
    """``sessions`` transcript files (default: exactly the default evidence
    floor); ``events`` go in the first, the rest hold one Bash call."""
    pdir = surface.claude_project_dir(ws, None if config_dir is None
                                      else {"CLAUDE_CONFIG_DIR": str(config_dir)})
    pdir.mkdir(parents=True, exist_ok=True)
    first = None
    for i in range(sessions):
        f = pdir / f"s{i + 1}.jsonl"
        body = events if i == 0 else [_BASH_CALL]
        f.write_text("\n".join(json.dumps(e) for e in body) + "\n", encoding="utf-8")
        os.utime(f, (2_000_000_000, 2_000_000_000))  # newer than any skill file
        first = first or f
    return first


def _skill_call(name: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": name}}]}}


_BASH_CALL = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "ls"}}]}}


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return root


# ================================================================ discovery
def test_user_and_local_scope_servers_are_discovered(ws):
    home = _home()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"usersrv": {"command": "user-mcp"},
                       "shared": {"command": "user-version"}},
        "projects": {str(ws.resolve()): {"mcpServers": {
            "localsrv": {"command": "local-mcp"},
            "shared": {"command": "local-version"}}}},
    }), encoding="utf-8")
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "shared": {"command": "project-version"},
        "remote": {"type": "sse", "url": "https://mcp.example.com/sse?token=abc"}}}),
        encoding="utf-8")
    specs = {s.name: s for s in surface.mcp_server_specs(ws)}
    assert specs["usersrv"].scope == "user"
    assert specs["localsrv"].scope == "local"
    assert specs["shared"].command == "local-version"      # local > project > user
    assert specs["remote"].transport == "sse"
    recs = {r.id: r for r in surface.collect_surface(ws)}
    assert recs["mcp.remote"].transport == "sse"           # enumerated, not skipped
    assert "token=abc" not in recs["mcp.remote"].detail    # never the full URL
    assert recs["mcp.usersrv"].scope == "user"
    # stdio servers only in the argv map; remote has no argv
    assert "remote" not in surface._mcp_server_commands(ws)


def test_claude_config_dir_relocates_user_config(ws, tmp_path, monkeypatch):
    cfg = tmp_path / "cfgdir"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"mcpServers": {"reloc": {"command": "x"}}}))
    _skill(cfg / "skills", "relocskill", "A relocated user skill.")
    (_home() / ".claude.json").write_text(json.dumps({"mcpServers": {"homesrv": {"command": "y"}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    names = {s.name for s in surface.mcp_server_specs(ws)}
    assert "reloc" in names and "homesrv" not in names
    recs = {r.id: r for r in surface.collect_surface(ws)}
    assert recs["skill.relocskill"].scope == "user"


def test_user_skills_agents_and_codex_user_config(ws):
    home = _home()
    _skill(home / ".claude" / "skills", "userskill", "User level skill.")
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / ".claude" / "agents" / "helper.md").write_text("# helper agent\n")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.codexsrv]\ncommand = "codex-mcp"\n[mcp_servers.off]\ncommand = "z"\nenabled = false\n')
    recs = {r.id: r for r in surface.collect_surface(ws)}
    assert recs["skill.userskill"].scope == "user"
    assert recs["agent.helper"].scope == "user"
    assert recs["mcp.codexsrv"].scope == "user"
    assert "mcp.off" not in recs


def test_discovery_never_writes_user_scope(ws):
    home = _home()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"u": {"command": "u"}}}))
    _skill(home / ".claude" / "skills", "s", "d")
    (home / ".claude" / "plugins").mkdir()
    before = _tree(home)
    surface.audit(ws)
    surface.collect_surface(ws)
    assert _tree(home) == before
    gaps = surface.discovery_gaps(ws)
    assert any("plugins" in g and "not enumerated" in g for g in gaps)


def test_codex_home_is_declared_not_guessed(ws, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert any("CODEX_HOME" in g for g in surface.discovery_gaps(ws))


# ================================================================ pricing
def test_skill_costs_its_listing_entry_not_its_file(ws):
    p = _skill(ws / ".claude" / "skills", "big", "Short description.", body="x" * 40_000)
    (p.parent / "reference.md").write_text("y" * 8000)
    _skill(ws / ".claude" / "skills", "manual", "Only by slash command.",
           extra="disable-model-invocation: true\n")
    recs = {r.id: r for r in surface.collect_surface(ws)}
    big = recs["skill.big"]
    assert big.tokens < 20 and big.deferred_tokens >= 10_000
    assert big.pricing == "skill-listing"
    assert recs["skill.reference"].tokens == 0
    assert recs["skill.reference"].pricing == "skill-resource"
    assert recs["skill.manual"].tokens == 0


def test_listing_capped_like_the_host():
    info = surface.skill_listing(Path("s/SKILL.md"), "---\nname: s\ndescription: " + "d" * 5000 + "\n---\nbody")
    assert len(info["entry"]) == surface.SKILL_LISTING_CAP_CHARS


def test_skill_without_frontmatter_uses_first_paragraph():
    info = surface.skill_listing(Path("skills/foo/SKILL.md"), "# Foo\nDoes foo.\n\n" + "Long body " * 50)
    assert info["name"] == "foo" and "Does foo." in info["entry"]
    assert "Long body" not in info["entry"]


_FAKE_MCP = (
    "import sys,json,os\n"
    "for line in sys.stdin:\n"
    " line=line.strip()\n"
    " if not line: continue\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'f'},'instructions':'Use me for files.'}}),flush=True)\n"
    " elif meth=='tools/list': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[\n"
    "   {'name':'read_file','description':'Read a file from disk','inputSchema':{'type':'object','properties':{'p':{'type':'string','description':'z'*800}}}},\n"
    "   {'name':'delete_file','description':'Delete a file','inputSchema':{'type':'object'}},\n"
    "   {'name':'env_check','description':'Report environment','inputSchema':{'type':'object'}}]}}),flush=True)\n"
    " elif meth=='tools/call':\n"
    "  n=m['params']['name']\n"
    "  if n=='env_check': txt='token-ok' if os.environ.get('FS_TOKEN')==os.environ.get('EXPECT') else 'token-missing'; txt+=' cwd='+os.path.basename(os.getcwd())\n"
    "  else: txt='did '+n\n"
    "  print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'content':[{'type':'text','text':txt}]}}),flush=True)\n"
)


def test_probed_mcp_prices_both_inline_and_deferred(ws):
    fake = ws / "fake.py"
    fake.write_text(_FAKE_MCP)
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "files": {"command": sys.executable, "args": [str(fake)]}}}))
    _approve(ws)
    a = surface.audit(ws, probe_mcp=True)
    assert a["schema"] == "ctx.surface/v2"
    tools = {r["id"]: r for r in a["records"] if r["kind"] == "mcp_tool"}
    rf = tools["mcp.files.read_file"]
    assert rf["upfront_deferred"] < rf["tokens"]           # name only vs full schema
    instr = next(r for r in a["records"] if r["id"] == "mcp.files")
    assert instr["pricing"] == "mcp-instructions" and instr["tokens"] > 0
    up = a["totals"]["upfront_estimate"]
    assert up["mcp_deferred"] < up["mcp_inline"] == a["totals"]["static_tokens"]
    assert "host-dependent" in up["which_applies"]


# ================================================================ gateway
@pytest.fixture()
def gw_ws(ws, monkeypatch):
    fake = ws / "fake.py"
    fake.write_text(_FAKE_MCP)
    sub = ws / "srvdir"
    sub.mkdir()
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "files": {"command": sys.executable, "args": [str(fake)],
                  "env": {"FS_TOKEN": "${MY_SECRET}"}, "cwd": str(sub)}}}))
    monkeypatch.setenv("MY_SECRET", SECRET)
    monkeypatch.setenv("EXPECT", SECRET)
    return ws


def _all_text(res) -> str:
    return json.dumps(res)


def test_gateway_backend_keeps_env_and_cwd_without_printing_values(gw_ws, capfd):
    g = gw.Gateway(gw_ws)
    outputs = [g.call("surface_index", {}), g.call("surface_index", {"query": "environment"}),
               g.call("surface_reveal", {"target": "files"})]
    res, _ = g.call("mcp__files__env_check", {})
    assert res["content"][0]["text"] == "token-ok cwd=srvdir"
    for r in outputs:
        assert SECRET not in _all_text(r)
    assert SECRET not in repr(g.specs["files"])
    g.close()
    cap = capfd.readouterr()
    assert SECRET not in cap.out and SECRET not in cap.err


def test_snapshot_rehydrates_env_from_live_config_never_stores_it(gw_ws, tmp_path):
    snap = tmp_path / "backends.json"
    spec = next(s for s in surface.mcp_server_specs(gw_ws) if s.name == "files")
    snap.write_text(json.dumps({"mcpServers": {"files": {
        "command": spec.command, "args": list(spec.args), "env_keys": ["FS_TOKEN"]}}}))
    g = gw.Gateway(gw_ws, backends_file=snap, persist=False)
    g.call("surface_reveal", {"target": "files"})
    res, _ = g.call("mcp__files__env_check", {})
    assert res["content"][0]["text"].startswith("token-ok")
    g.close()
    assert not (gw_ws / gw.STATE_FILE).exists()        # ephemeral: no state file


def test_query_finds_a_tool_without_revealing_it(gw_ws):
    g = gw.Gateway(gw_ws)
    res, changed = g.call("surface_index", {"query": "delete"})
    text = res["content"][0]["text"]
    assert "mcp__files__delete_file" in text and changed is False
    assert {t["name"] for t in g.visible_tools()} == {"surface_index", "surface_reveal", "surface_hide"}
    g.close()


def test_reveal_one_tool_then_server_then_hide(gw_ws):
    g = gw.Gateway(gw_ws)
    _, changed = g.call("surface_reveal", {"target": "mcp__files__read_file"})
    assert changed
    names = {t["name"] for t in g.visible_tools()}
    assert "mcp__files__read_file" in names and "mcp__files__delete_file" not in names
    res, _ = g.call("mcp__files__delete_file", {})
    assert "hidden" in res["content"][0]["text"]
    g.call("surface_reveal", {"target": "files"})
    assert "mcp__files__delete_file" in {t["name"] for t in g.visible_tools()}
    _, changed = g.call("surface_hide", {"target": "files"})
    assert changed
    assert "mcp__files__delete_file" not in {t["name"] for t in g.visible_tools()}
    # family-only writers (reconcile) keep the finer-grained reveal
    gw.save_state(gw_ws, {"harness"})
    assert ["files", "read_file"] in json.loads((gw_ws / gw.STATE_FILE).read_text())["tools"]
    g.close()


def test_profile_excluded_tool_is_never_listed(gw_ws, tmp_path):
    spec = next(s for s in surface.mcp_server_specs(gw_ws) if s.name == "files")
    snap = tmp_path / "b.json"
    snap.write_text(json.dumps({"mcpServers": {"files": {
        "command": spec.command, "args": list(spec.args), "exclude_tools": ["delete_file"]}}}))
    g = gw.Gateway(gw_ws, backends_file=snap, persist=False)
    g.call("surface_reveal", {"target": "files"})
    names = {t["name"] for t in g.visible_tools()}
    assert "mcp__files__read_file" in names and "mcp__files__delete_file" not in names
    assert "delete_file" not in g.call("surface_index", {"query": "delete"})[0]["content"][0]["text"]
    g.close()


def test_gateway_schema_stays_three_tools_and_small():
    assert [t["name"] for t in gw._META_TOOLS] == ["surface_index", "surface_reveal", "surface_hide"]
    assert len(json.dumps(gw._META_TOOLS).encode()) <= 900


# ================================================================ plan
def test_no_usage_evidence_changes_nothing(ws):
    _skill(ws / ".claude" / "skills", "a", "Alpha.")
    plan = sl.plan_claude_launch(ws, ["-p", "x"], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings == {} and plan.args == []
    assert any("no usage evidence" in x["reason"] for x in plan.receipt["left_alone"])


def test_unused_skills_go_name_only_used_and_ctx_skill_stay(ws):
    sk = ws / ".claude" / "skills"
    _skill(sk, "unused", "Never called.")
    _skill(sk, "used", "Called often.")
    _skill(sk, "ctx-harness", "ctx's own.")
    _skill(_home() / ".claude" / "skills", "userunused", "User skill never called.")
    _transcript(ws, [_skill_call("used"), _BASH_CALL])
    plan = sl.plan_claude_launch(ws, ["-p", "x"], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings["skillOverrides"] == {"unused": "name-only", "userunused": "name-only"}
    items = {i["name"]: i for i in plan.receipt["skills"]["items"]}
    assert items["used"]["invocations"] == 1 and items["used"]["mechanism"] == "unchanged"
    assert "ctx's own" in items["ctx-harness"]["reason"]
    assert "off" not in plan.settings["skillOverrides"].values()
    t = plan.receipt["tokens_est"]["skills"]
    assert t["after"] < t["before"]


def test_below_evidence_floor_nothing_changes(ws):
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    _transcript(ws, [_BASH_CALL], sessions=4)
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings == {}
    item = plan.receipt["skills"]["items"][0]
    assert item["mechanism"] == "unchanged"
    assert "insufficient usage evidence: 4 of 5" in item["reason"]
    assert plan.receipt["skills"]["min_sessions"] == 5
    assert plan.receipt["skills"]["floor_met"] is False
    assert "below floor" in plan.line and "est." not in plan.line


def test_at_floor_demotes(ws):
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    _transcript(ws, [_BASH_CALL], sessions=5)
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings["skillOverrides"] == {"unused": "name-only"}
    assert plan.receipt["skills"]["floor_met"] is True


@pytest.mark.parametrize("value,expect", [("2", 2), ('"lots"', 5), ("-3", 5), ("0", 5)])
def test_floor_config_key(ws, value, expect):
    (ws / "ctx.toml").write_text(f"version = 1\n[surface]\nwrap_min_sessions = {value}\n")
    assert sl.min_sessions_floor(ws) == expect
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    _transcript(ws, [_BASH_CALL], sessions=2)
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert ("skillOverrides" in plan.settings) is (expect <= 2)


def test_floor_counts_sessions_actually_read(ws, monkeypatch):
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    _transcript(ws, [_BASH_CALL], sessions=6)
    real = surface.transcript_usage
    monkeypatch.setattr(surface, "transcript_usage",
                        lambda root, env=None: real(root, env=env, max_files=3))
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings == {}
    assert "3 of 5" in plan.receipt["skills"]["items"][0]["reason"]


def test_emitted_overrides_carry_user_entries_forward(ws):
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    (ws / ".claude" / "settings.json").write_text(json.dumps({"skillOverrides": {"other": "off"}}))
    _transcript(ws, [_BASH_CALL])
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings["skillOverrides"] == {"other": "off", "unused": "name-only"}


def test_user_skill_override_wins(ws):
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    (_home() / ".claude").mkdir(parents=True, exist_ok=True)
    (_home() / ".claude" / "settings.json").write_text(json.dumps({"skillOverrides": {"unused": "on"}}))
    _transcript(ws, [_BASH_CALL])
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings == {}
    assert any(x["item"] == "unused" for x in plan.receipt["left_alone"])


@pytest.mark.parametrize("args,env,dimension", [
    (["--mcp-config", "mine.json"], {}, "mcp"),
    (["--strict-mcp-config"], {}, "mcp"),
    (["--settings", "mine.json"], {}, "skills"),
    ([], {"SLASH_COMMAND_TOOL_CHAR_BUDGET": "4000"}, "skills"),
    ([], {"ENABLE_TOOL_SEARCH": "false"}, "mcp"),
])
def test_caller_wins(ws, args, env, dimension):
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "fs-mcp"}}}))
    _approve(ws)
    _transcript(ws, [_BASH_CALL])
    penv = {**os.environ, **env}
    plan = sl.plan_claude_launch(ws, args, mode="local-dev", child_env=penv, parent_env=penv,
                                 tmpdir=ws.parent)
    dims = {x["dimension"] for x in plan.receipt["left_alone"]}
    assert dimension in dims
    if dimension == "skills":
        assert "skillOverrides" not in plan.settings
    if args[:1] in (["--mcp-config"], ["--strict-mcp-config"]):
        assert "--mcp-config" not in plan.args
        assert all(s["mechanism"] == "left-alone" for s in plan.receipt["mcp"]["servers"])


@pytest.mark.parametrize("env,state", [
    ({}, "deferred"),
    ({"ENABLE_TOOL_SEARCH": "true"}, "deferred"),
    ({"ENABLE_TOOL_SEARCH": "false"}, "inline"),
    ({"ENABLE_TOOL_SEARCH": "auto:5"}, "conditional"),
    ({"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"}, "inline"),
    ({"ANTHROPIC_BASE_URL": "https://gateway.corp.example"}, "inline"),
    ({"ANTHROPIC_BASE_URL": "http://127.0.0.1:9999", "ENABLE_TOOL_SEARCH": "true"}, "deferred"),
])
def test_deferral_detection(env, state):
    assert sl.deferral_state(env)[0] == state


def test_always_load_server_is_reported_inline(ws):
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "pinned": {"command": "p", "alwaysLoad": True}, "lazy": {"command": "l"}}}))
    _approve(ws)
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    mech = {s["name"]: s["mechanism"] for s in plan.receipt["mcp"]["servers"]}
    assert mech == {"pinned": "host-inline", "lazy": "host-deferred"}
    assert plan.args == []    # auto never interposes the gateway


def test_managed_mcp_config_blocks_mcp_config_flag(ws, tmp_path, monkeypatch):
    managed = tmp_path / "managed-mcp.json"
    managed.write_text("{}")
    monkeypatch.setattr(surface, "MANAGED_MCP_PATHS", (str(managed),))
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}))
    plan = sl.plan_claude_launch(ws, [], mode="local-dev", child_env={},
                                 parent_env=dict(os.environ), tmpdir=tmp_path)
    assert "--mcp-config" not in plan.args
    assert any("managed MCP config" in x["reason"] for x in plan.receipt["left_alone"])


# ================================================================ wrapper
def _fake_claude(bin_dir: Path, rec: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(f"""#!/bin/sh
if [ "$1" = "--help" ]; then echo "usage: claude [--settings <file>]"; exit 0; fi
printf '%s\\n' "$@" > {rec}/argv.txt
prev=""
for a in "$@"; do
  if [ "$prev" = "--settings" ]; then cat "$a" > {rec}/settings.json; fi
  if [ "$prev" = "--mcp-config" ]; then cat "$a" > {rec}/mcp.json; fi
  prev="$a"
done
for f in "$(dirname "$(sed -n '/--mcp-config/{{n;p}}' {rec}/argv.txt)")"/backends.json; do
  [ -f "$f" ] && cat "$f" > {rec}/backends.json
done
exit 0
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def launcher(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    _fake_claude(tmp_path / "bin", rec)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    return rec


def test_print_mode_stdout_clean_and_zero_residue(ws, launcher, capfd):
    from ctx.wrap import wrap_claude

    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "fs": {"command": "fs-mcp"}, "docs": {"type": "http", "url": "https://x.example/mcp"}}}))
    _approve(ws)
    _transcript(ws, [_BASH_CALL])
    home_before, ws_before = _tree(_home()), _tree(ws)
    rc = wrap_claude(ws, ["-p", "do it"], "/opt/bin/ctx")
    assert rc == 0
    cap = capfd.readouterr()
    assert cap.out == ""                                   # print mode: stdout untouched
    assert "ctx wrap: surface auto" in cap.err and "est." in cap.err
    assert json.loads((launcher / "settings.json").read_text())["skillOverrides"] == {"unused": "name-only"}
    assert _tree(_home()) == home_before                   # HOME unchanged
    after = _tree(ws)
    added = set(after) - set(ws_before)
    assert added == {sl.RECEIPT_FILE}                      # only the documented receipt
    assert all(after[k] == v for k, v in ws_before.items())
    receipt = json.loads(after[sl.RECEIPT_FILE])
    assert receipt["schema"] == "ctx.surface.wrap/v1"
    assert "ESTIMATES" in receipt["estimates"]
    mech = {s["name"]: s for s in receipt["mcp"]["servers"]}
    assert "passed through unchanged" in mech["docs"]["reason"]


def test_interactive_mode_works_and_off_does_nothing(ws, launcher, capfd):
    from ctx.wrap import wrap_claude

    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    _transcript(ws, [_BASH_CALL])
    assert wrap_claude(ws, [], "/opt/bin/ctx") == 0
    assert "skillOverrides" in json.loads((launcher / "settings.json").read_text())
    (ws / sl.RECEIPT_FILE).unlink()
    assert wrap_claude(ws, [], "/opt/bin/ctx", surface="off") == 0
    assert "skillOverrides" not in json.loads((launcher / "settings.json").read_text())
    assert not (ws / sl.RECEIPT_FILE).exists()
    assert capfd.readouterr().err.count("ctx wrap: surface") == 1   # off prints nothing


def test_config_key_selects_mode(ws, launcher):
    from ctx.wrap import wrap_claude

    (ws / "ctx.toml").write_text('version = 1\n[surface]\nwrap = "off"\n')
    _skill(ws / ".claude" / "skills", "unused", "Never called.")
    _transcript(ws, [_BASH_CALL])
    wrap_claude(ws, ["-p", "x"], "/opt/bin/ctx")
    assert "skillOverrides" not in json.loads((launcher / "settings.json").read_text())


def test_profile_launch_uses_temp_mcp_config_and_gateway(ws, launcher, tmp_path, monkeypatch):
    from ctx.wrap import wrap_claude

    (ws / "ctx.toml").write_text("version = 1\n[surface]\ngateway = true\n")
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp", "args": ["serve"]},
        "pytest-runner": {"command": "pytest-mcp", "env": {"RUNNER_KEY": SECRET}},
        "docs": {"type": "http", "url": "https://docs.example/mcp"}}}))
    _approve(ws)
    rc = wrap_claude(ws, ["-p", "x"], "/opt/bin/ctx", surface="local-dev")
    assert rc == 0
    argv = (launcher / "argv.txt").read_text().splitlines()
    i = argv.index("--mcp-config")
    assert argv[i + 2] == "--strict-mcp-config"          # ends the variadic flag
    assert not os.path.exists(argv[i + 1])                 # temp config removed
    mcp = json.loads((launcher / "mcp.json").read_text())["mcpServers"]
    assert "github" not in mcp                             # dropped by the profile
    assert sl.GATEWAY_NAME in mcp
    backends = json.loads((launcher / "backends.json").read_text())["mcpServers"]
    assert backends["pytest-runner"]["env_keys"] == ["RUNNER_KEY"]
    assert SECRET not in (launcher / "backends.json").read_text()
    receipt = (ws / sl.RECEIPT_FILE).read_text()
    assert SECRET not in receipt
    rows = {s["name"]: s["mechanism"] for s in json.loads(receipt)["mcp"]["servers"]}
    assert rows["github"] == "dropped-by-profile" and rows["pytest-runner"] == "behind-gateway"


def test_surface_failure_never_blocks_launch(ws, launcher, monkeypatch, capfd):
    from ctx.wrap import wrap_claude

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(sl, "plan_claude_launch", boom)
    assert wrap_claude(ws, ["-p", "x"], "/opt/bin/ctx") == 0
    assert "surface plan skipped" in capfd.readouterr().err
    assert (launcher / "argv.txt").is_file()


def test_cmd_wrap_parses_surface_flag(ws, monkeypatch):
    from argparse import Namespace

    import ctx.wrap as wrap_mod
    from ctx.commands.hosts import cmd_wrap

    calls = []
    monkeypatch.setattr(wrap_mod, "wrap_claude", lambda *a, **k: calls.append((a, k)) or 0)
    monkeypatch.chdir(ws)
    ns = Namespace(host="claude", agent_args=["--surface", "review", "--", "-p", "--surface", "x"],
                   print_config=False, workspace=str(ws))
    assert cmd_wrap(ns) == 0
    args, kwargs = calls[-1]
    assert kwargs["surface"] == "review" and args[1] == ["-p", "--surface", "x"]
    ns = Namespace(host="claude", agent_args=["--surface=off", "-p", "y"],
                   print_config=False, workspace=str(ws))
    cmd_wrap(ns)
    assert calls[-1][1]["surface"] == "off" and calls[-1][0][1] == ["-p", "y"]


# ================================================================ usage
def test_transcript_usage_counts_skill_tool_and_slash_commands(ws):
    _transcript(ws, [
        _skill_call("alpha"), _skill_call("plug:beta"),
        {"type": "user", "message": {"content": "<command-name>/gamma</command-name>"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "m", "name": "mcp__gh__search", "input": {}}]}},
    ])
    u = surface.transcript_usage(ws)
    assert u["skills"] == {"alpha": 1, "gamma": 1, "plug:beta": 1}
    assert u["mcp_tools"] == {"mcp__gh__search": 1}
    assert sl._invocations("beta", u) == 1


def test_no_transcripts_is_none_not_zero(ws):
    assert surface.transcript_usage(ws) is None


def test_antigravity_skill_priced_as_listing_and_hosts_declared(ws):
    _skill(ws / ".agents" / "skills", "agyskill", "Antigravity skill.", body="b" * 20_000)
    rec = {r.id: r for r in surface.collect_surface(ws)}["skill.agyskill"]
    assert rec.pricing == "skill-listing" and rec.tokens < 20 and rec.deferred_tokens > 4000
    by_host = surface.audit(ws)["totals"]["upfront_estimate"]["by_host"]
    assert set(by_host) == {"claude", "codex", "antigravity"}


def test_project_dir_sanitization(ws):
    d = surface.claude_project_dir(ws)
    assert d.name == re.sub(r"[^A-Za-z0-9]", "-", str(ws.resolve()))
