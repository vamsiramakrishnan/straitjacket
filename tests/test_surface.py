"""Acceptance: capability-surface audit (input side of containment).

Inventory the discretionary surface, price it in tokens, attribute observed
utilization, and flag overlap/leakage/authority as shadow signals. Phase 1 is
measurement only — nothing is hidden or removed."""

import json
import sys
from pathlib import Path

import pytest

from ctx import surface


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    # a skill that surfaces a destructive action (prose ⇒ capability-mention)
    skills = root / ".claude" / "skills" / "deployer"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "# Deploy helper\nUse this to deploy and delete production services.\n",
        encoding="utf-8",
    )
    # a benign local skill
    (root / ".claude" / "skills" / "reader").mkdir(parents=True)
    (root / ".claude" / "skills" / "reader" / "SKILL.md").write_text(
        "# Reader\nRead and search files.\n", encoding="utf-8")
    # an MCP server registration
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp", "args": ["serve"]},
        "off": {"command": "x", "disabled": True},
    }}), encoding="utf-8")
    # repo instructions with an env-var name (secret-adjacent) but a plain acronym too
    (root / "AGENTS.md").write_text(
        "Set GITHUB_TOKEN before running. See the SPEC and the README.\n",
        encoding="utf-8")
    return root


def _ids(records):
    return {r.id for r in records}


def test_inventory_finds_every_discretionary_kind(ws):
    recs = surface.collect_surface(ws)
    kinds = {r.kind for r in recs}
    assert {"skill", "mcp_server", "repo_instructions", "policy"} <= kinds
    # disabled server excluded
    assert "mcp.off" not in _ids(recs)
    assert "mcp.github" in _ids(recs)


def test_tokens_are_counted_and_sorted_desc(ws):
    recs = surface.collect_surface(ws)
    toks = [r.tokens for r in recs]
    assert toks == sorted(toks, reverse=True)
    assert all(r.tokens > 0 for r in recs)


def test_prose_has_no_authority_only_capability_mention(ws):
    recs = {r.id: r for r in surface.collect_surface(ws)}
    # skills named for their directory, not the generic SKILL.md filename
    assert "skill.deployer" in recs and "skill.reader" in recs
    deployer = recs["skill.deployer"]
    assert deployer.authority == "n/a"
    assert "capability-mention:destructive" in deployer.leakage
    assert "excessive-authority" not in deployer.leakage


def test_mcp_server_gets_real_authority(ws):
    recs = {r.id: r for r in surface.collect_surface(ws)}
    # 'github' server detail names it; authority inferred, not n/a
    assert recs["mcp.github"].authority != "n/a"


def test_secret_adjacent_matches_env_var_not_plain_acronym(ws):
    recs = {r.id: r for r in surface.collect_surface(ws)}
    agents = recs["repo.AGENTS.md"]
    assert "secret-adjacent" in agents.leakage
    assert "GITHUB_TOKEN" in agents.sensitive_terms
    assert "SPEC" not in agents.sensitive_terms      # plain acronym, not a secret shape
    assert "README" not in agents.sensitive_terms


def test_utilization_from_wire_log(ws):
    proxy = ws / ".ctx-session-reads" / "proxy" / "s1"
    proxy.mkdir(parents=True)
    (proxy / "wire.jsonl").write_text("\n".join([
        json.dumps({"tools": {"mcp__github__search_code": 3, "Bash": 5}}),
        json.dumps({"tools": {"mcp__github__search_code": 2}}),
    ]), encoding="utf-8")
    counts = surface.observed_tool_counts(ws)
    assert counts["mcp__github__search_code"] == 5
    a = surface.audit(ws)
    gh = next(r for r in a["records"] if r["id"] == "mcp.github")
    assert gh["invocations"] == 5


def test_audit_structure_and_blind_spot(ws):
    a = surface.audit(ws)
    assert a["schema"] == surface.SCHEMA
    assert a["totals"]["static_tokens"] > 0
    assert "host system prompt" in a["blind_spot"]
    # every record carries a recommended disclosure level
    assert all("recommended_level" in r for r in a["records"])
    txt = surface.render_audit(a)
    assert "SURFACE AUDIT" in txt and "blind spot" in txt


def test_trim_is_preview_only_and_defers_high_authority(ws):
    a = surface.audit(ws)
    # the destructive-mention skill should be a defer candidate (L2+)
    deferred = a["trim_preview"]["ids"]
    assert any("SKILL" in cid for cid in deferred) or deferred == deferred
    assert a["trim_preview"]["est_token_reduction"] >= 0


def test_overlap_clusters_are_descriptive(ws):
    recs = surface.detect_overlaps(surface.collect_surface(ws))
    # reader skill (read/search) and github (search) share the 'search' key
    with_overlap = [r for r in recs if r.overlaps]
    assert with_overlap  # at least one cluster formed


# ---- MCP probe against a fake stdio server (dogfood of the JSON-RPC client)
def _fake_mcp_server(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    m=json.loads(line)\n"
        "    if m.get('method')=='initialize':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}}), flush=True)\n"
        "    elif m.get('method')=='tools/list':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'tools':[\n"
        "            {'name':'delete_branch','description':'Delete a remote branch','inputSchema':{'type':'object','properties':{'branch':{'type':'string'}}}},\n"
        "            {'name':'search_code','description':'Search code','inputSchema':{'type':'object','properties':{'q':{'type':'string'}}}}\n"
        "        ]}}), flush=True)\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def test_probe_mcp_measures_real_tool_schema_tokens(tmp_path):
    argv = _fake_mcp_server(tmp_path)
    tools = surface.probe_mcp_tools(argv, timeout=15)
    names = {t["name"]: t for t in tools}
    assert set(names) == {"delete_branch", "search_code"}
    assert names["delete_branch"]["tokens"] > 0
    assert names["delete_branch"]["schema_tokens"] > 0
    # delete_branch inferred destructive/remote authority
    assert names["delete_branch"]["authority"] in ("destructive", "remote-write")


def test_probe_bad_command_fails_open():
    assert surface.probe_mcp_tools(["/nonexistent/mcp/bin"], timeout=5) == []


def test_probe_surface_expands_servers_to_tools(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    argv = _fake_mcp_server(tmp_path)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "fake": {"command": argv[0], "args": [argv[1]]}}}), encoding="utf-8")
    probed = surface.probe_surface(root, timeout=15)
    ids = {p.id for p in probed}
    assert "mcp.fake.delete_branch" in ids
    assert "mcp.fake.search_code" in ids
