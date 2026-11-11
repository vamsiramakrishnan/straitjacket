"""Acceptance: the SessionStart pre-flight gate ('bound before bloat') and the
probe cache that makes it cheap every session after the first."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ctx import surface

SRC = Path(__file__).resolve().parent.parent / "src"

_FAKE = (
    "import sys,json\n"
    "for line in sys.stdin:\n"
    " line=line.strip()\n"
    " if not line: continue\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'big','version':'1'}}}),flush=True)\n"
    " elif meth=='tools/list': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[\n"
    "   {'name':'big_tool','description':'x'*4000,'inputSchema':{'type':'object','properties':{'q':{'type':'string','description':'y'*2000}}}}]}}),flush=True)\n"
)


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    fake = root / "big_mcp.py"
    fake.write_text(_FAKE, encoding="utf-8")
    (root / "ctx.toml").write_text("version=1\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "big": {"command": sys.executable, "args": [str(fake)]}}}), encoding="utf-8")
    # approved, as after the trust prompt (unapproved project servers cost 0)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text(
        json.dumps({"enableAllProjectMcpServers": True}), encoding="utf-8")
    return root


@pytest.fixture()
def inline_host(monkeypatch):
    """These fixtures price a host that INLINES schemas. Deliberate update:
    the gate now prices on the basis that applies (Claude Code defers by
    default), so the inline case is selected explicitly."""
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")


def test_preflight_fires_over_budget(ws, inline_host):
    adv = surface.preflight(ws, max_static_tokens=100)
    assert "CTX_SURFACE_GUARD" in adv
    assert "inline basis" in adv
    assert "mcp_tool" in adv          # the big tool schema is the heaviest kind
    assert "compile --profile" in adv


def test_preflight_uses_deferred_basis_on_deferring_host(ws, monkeypatch):
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", raising=False)
    a = surface.audit(ws, probe_mcp=True)
    deferred = a["totals"]["upfront_estimate"]["mcp_deferred"]
    inline = a["totals"]["static_tokens"]
    assert deferred < inline
    # over the deferred figure but under the inline one: fires, quoting deferred
    adv = surface.preflight(ws, max_static_tokens=deferred - 1)
    assert f"~{deferred:,} tokens up front" in adv and "deferred basis" in adv
    assert f"~{inline:,} if every schema were inlined" in adv
    assert surface.preflight(ws, max_static_tokens=deferred) == ""


def test_preflight_silent_under_budget(ws):
    assert surface.preflight(ws, max_static_tokens=1_000_000) == ""


def test_preflight_gateway_message(ws, inline_host):
    adv = surface.preflight(ws, max_static_tokens=100, gateway=True)
    assert "gateway active" in adv


def test_preflight_fail_open_on_bad_workspace():
    assert surface.preflight("/no/such/dir", max_static_tokens=1) == ""


def test_probe_cache_written_and_reused(ws):
    surface.probe_surface(ws)  # first probe spawns + caches
    cache = json.loads((ws / surface._PROBE_CACHE).read_text())
    assert "big" in cache and cache["big"]["tools"]
    # corrupt the fake server so a live re-probe would fail; cache still serves
    (ws / "big_mcp.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    # same argv → cache hit → tools still returned without a live probe
    recs = surface.probe_surface(ws)
    assert any(r.id == "mcp.big.big_tool" for r in recs)


def test_probe_cache_invalidated_on_argv_change(ws):
    surface.probe_surface(ws)
    # change the server command → cache key differs → live re-probe (which now
    # points at a broken command) yields nothing for that server
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "big": {"command": sys.executable, "args": ["-c", "import sys;sys.exit(1)"]}}}),
        encoding="utf-8")
    recs = surface.probe_surface(ws)
    assert not any(r.provider == "big" for r in recs)


#: Repo instructions are loaded whole, up front, on EVERY host and either MCP
#: basis (~550 tokens here), so a budget of 100 is exceeded by construction —
#: not by whatever the fixture's MCP schema happens to cost on the basis that
#: applies (deferred on all three hosts, where it is a tool name only).
_INSTRUCTIONS = "Always run the full test suite before you finish. " * 44


def _hook(host, ws, budget, tmp_path):
    (ws / "ctx.toml").write_text(
        f"version=1\n[surface]\nmax_static_tokens = {budget}\n", encoding="utf-8")
    (ws / "AGENTS.md").write_text(_INSTRUCTIONS, encoding="utf-8")
    payload = json.dumps({"hook_event_name": "SessionStart",
                          "cwd": str(ws), "workspacePaths": [str(ws)]})
    # Hermetic: an empty HOME and state dir of our own. Without HOME the
    # child falls back to the account's real home (the password database)
    # and prices whatever user-scope skills and servers live there.
    home = tmp_path / "hook-home"
    home.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", host, "session-start"],
        input=payload.encode(),
        env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", ""),
             "HOME": str(home), "CTX_STATE_HOME": str(tmp_path / "hook-state")},
        capture_output=True, timeout=40)
    return json.loads(proc.stdout.decode().strip())


def test_hook_fixture_exceeds_budget_on_the_applicable_basis(ws):
    (ws / "AGENTS.md").write_text(_INSTRUCTIONS, encoding="utf-8")
    a = surface.audit(ws)
    assert a["totals"]["upfront_estimate"]["mcp_deferred"] > 100
    assert a["totals"]["by_kind"]["repo_instructions"]["tokens"] > 100


def test_sessionstart_claude_injects_advisory_over_budget(ws, tmp_path):
    out = _hook("claude-code", ws, 100, tmp_path)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx_text = out["hookSpecificOutput"]["additionalContext"]
    assert "CTX_SURFACE_GUARD" in ctx_text and "deferred basis: Claude Code" in ctx_text


def test_sessionstart_claude_noop_under_budget(ws, tmp_path):
    out = _hook("claude-code", ws, 1_000_000, tmp_path)
    assert out == {"continue": True}


def test_sessionstart_codex_injects_advisory(ws, tmp_path):
    out = _hook("codex", ws, 100, tmp_path)
    ctx_text = out["hookSpecificOutput"]["additionalContext"]
    assert "CTX_SURFACE_GUARD" in ctx_text and "deferred basis: Codex" in ctx_text


def test_preinvocation_antigravity_shape(ws, tmp_path):
    # Antigravity has no SessionStart event; the advisory rides PreInvocation's
    # injectSteps as an ephemeral message so it does not accumulate in the
    # transcript on every invocation.
    over = _hook("antigravity", ws, 100, tmp_path)
    assert "CTX_SURFACE_GUARD" in over["injectSteps"][0]["ephemeralMessage"]
    assert "deferred basis: Antigravity" in over["injectSteps"][0]["ephemeralMessage"]
    assert "additionalContext" not in over
    under = _hook("antigravity", ws, 1_000_000, tmp_path)
    assert under == {}


def test_gate_off_is_silent(ws):
    (ws / "ctx.toml").write_text(
        "version=1\n[surface]\nmax_static_tokens = 1\ngate = \"off\"\n", encoding="utf-8")
    payload = json.dumps({"cwd": str(ws), "workspacePaths": [str(ws)]})
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", "claude-code", "session-start"],
        input=payload.encode(),
        env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", ""),
             "HOME": __import__("os").environ["HOME"]},  # the isolated test HOME
        capture_output=True, timeout=40)
    assert json.loads(proc.stdout.decode().strip()) == {"continue": True}


def test_surface_policy_parsed_from_ctx_toml(tmp_path):
    from ctx.config import load_config

    (tmp_path / "ctx.toml").write_text(
        "version=1\n[surface]\nmax_static_tokens = 4321\ngateway = true\n"
        "default_profile = \"read-only\"\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.surface.max_static_tokens == 4321
    assert cfg.surface.gateway is True
    assert cfg.surface.default_profile == "read-only"
