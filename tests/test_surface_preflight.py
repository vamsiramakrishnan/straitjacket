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
    return root


def test_preflight_fires_over_budget(ws):
    adv = surface.preflight(ws, max_static_tokens=100)
    assert "CTX_SURFACE_GUARD" in adv
    assert "mcp_tool" in adv          # the big tool schema is the heaviest kind
    assert "compile --profile" in adv


def test_preflight_silent_under_budget(ws):
    assert surface.preflight(ws, max_static_tokens=1_000_000) == ""


def test_preflight_gateway_message(ws):
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


def _hook(host, ws, budget):
    (ws / "ctx.toml").write_text(
        f"version=1\n[surface]\nmax_static_tokens = {budget}\n", encoding="utf-8")
    payload = json.dumps({"hook_event_name": "SessionStart",
                          "cwd": str(ws), "workspacePaths": [str(ws)]})
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", host, "session-start"],
        input=payload.encode(),
        env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True, timeout=40)
    return json.loads(proc.stdout.decode().strip())


def test_sessionstart_claude_injects_advisory_over_budget(ws):
    out = _hook("claude-code", ws, 100)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "CTX_SURFACE_GUARD" in out["hookSpecificOutput"]["additionalContext"]


def test_sessionstart_claude_noop_under_budget(ws):
    out = _hook("claude-code", ws, 1_000_000)
    assert out == {"continue": True}


def test_sessionstart_codex_injects_advisory(ws):
    out = _hook("codex", ws, 100)
    assert "CTX_SURFACE_GUARD" in out["hookSpecificOutput"]["additionalContext"]


def test_preinvocation_antigravity_shape(ws):
    # Antigravity has no SessionStart event; the advisory rides PreInvocation's
    # injectSteps as an ephemeral message so it does not accumulate in the
    # transcript on every invocation.
    over = _hook("antigravity", ws, 100)
    assert "CTX_SURFACE_GUARD" in over["injectSteps"][0]["ephemeralMessage"]
    assert "additionalContext" not in over
    under = _hook("antigravity", ws, 1_000_000)
    assert under == {}


def test_gate_off_is_silent(ws):
    (ws / "ctx.toml").write_text(
        "version=1\n[surface]\nmax_static_tokens = 1\ngate = \"off\"\n", encoding="utf-8")
    payload = json.dumps({"cwd": str(ws), "workspacePaths": [str(ws)]})
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", "claude-code", "session-start"],
        input=payload.encode(),
        env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", "")},
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
