"""Regression tests for the surface-on-wrap review findings.

Knowing a user/local-scope server exists must never, by itself, start it or
copy it into the workspace; displayed numbers name their basis; usage
evidence counts only real sessions; the gateway handles dotted names and
answers queries from the probe cache; launches clean up on signals."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ctx import surface
from ctx import surface_gateway as gw
from ctx import surface_launch as sl

SRC = Path(__file__).resolve().parent.parent / "src"
_ENV_VARS = ("CLAUDE_CONFIG_DIR", "ENABLE_TOOL_SEARCH", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
             "ANTHROPIC_BASE_URL", "SLASH_COMMAND_TOOL_CHAR_BUDGET", "CODEX_HOME")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for v in _ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(surface, "MANAGED_MCP_PATHS", (str(tmp_path / "none.json"),))


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n")
    return root


def _home() -> Path:
    return Path(os.environ["HOME"])


def _approve(ws: Path) -> None:
    (ws / ".claude").mkdir(exist_ok=True)
    (ws / ".claude" / "settings.local.json").write_text('{"enableAllProjectMcpServers": true}')


def _user_servers(servers: dict) -> None:
    (_home() / ".claude.json").write_text(json.dumps({"mcpServers": servers}))


# A server that records that it was started (and with which X), then speaks MCP.
_MARKER_MCP = (
    "import sys,json,os\n"
    "open(sys.argv[1],'a').write('started X=' + os.environ.get('X','') + '\\n')\n"
    "for line in sys.stdin:\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{}}}),flush=True)\n"
    " elif meth=='tools/list': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[{'name':'t','description':'d','inputSchema':{'type':'object'}}]}}),flush=True)\n"
)
_FAILING = "import sys\nopen(sys.argv[1],'a').write('x\\n')\n"


# ================================================================ 1 spawning
def test_session_start_never_starts_user_or_local_scope_servers(ws, tmp_path):
    script = tmp_path / "m.py"
    script.write_text(_MARKER_MCP)
    marker = tmp_path / "user-started"
    _user_servers({"usersrv": {"command": sys.executable, "args": [str(script), str(marker)]}})
    (_home() / ".claude.json").write_text(json.dumps({
        "mcpServers": {"usersrv": {"command": sys.executable, "args": [str(script), str(marker)]}},
        "projects": {str(ws.resolve()): {"mcpServers": {"localsrv": {
            "command": sys.executable, "args": [str(script), str(marker)]}}}}}))
    surface.preflight(ws, max_static_tokens=1)
    assert not marker.exists()
    # an explicit --probe-mcp does probe them
    surface.audit(ws, probe_mcp=True)
    assert marker.read_text().count("started") == 2


def test_automatic_probe_passes_no_configured_env(ws, tmp_path):
    script = tmp_path / "m.py"
    script.write_text(_MARKER_MCP)
    marker = tmp_path / "ws-started"
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"w": {
        "command": sys.executable, "args": [str(script), str(marker)], "env": {"X": "secret"}}}}))
    surface.preflight(ws, max_static_tokens=1)
    assert marker.read_text() == "started X=\n"


def test_failed_probe_is_remembered_by_automatic_callers(ws, tmp_path):
    script = tmp_path / "fail.py"
    script.write_text(_FAILING)
    count = tmp_path / "count"
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"bad": {
        "command": sys.executable, "args": [str(script), str(count)]}}}))
    surface.preflight(ws, max_static_tokens=1)
    surface.preflight(ws, max_static_tokens=1)
    assert count.read_text().count("x") == 1
    cache = surface.read_probe_cache(ws)
    assert cache["bad"]["failed_at"] > 0
    surface.audit(ws, probe_mcp=True)          # explicit: retried
    assert count.read_text().count("x") == 2


# ================================================================ 2 secrets
_SECRET_ARGS = ["--token=abc123SECRET", "postgres://user:pw0SECRET@db/x",
                "--api-key", "zzzSECRET", "https://h.example/p?key=qSECRET"]


def test_server_detail_never_shows_credential_arguments(ws):
    _user_servers({"db": {"command": "db-mcp", "args": _SECRET_ARGS}})
    rec = {r.id: r for r in surface.collect_surface(ws)}["mcp.db"]
    assert "SECRET" not in rec.detail
    assert "db-mcp" in rec.detail
    assert "SECRET" not in json.dumps(surface.audit(ws))


def test_workspace_files_never_receive_user_scope_definitions(ws, capsys):
    from ctx import surface_profiles as sp
    from ctx.installer import install_gateway
    from ctx.workspace import resolve_workspace

    _user_servers({"db": {"command": "db-mcp", "args": _SECRET_ARGS}})
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "fs-mcp"}}}))
    rep = sp.compile_profile(ws, "full", host="claude", apply=True)
    assert {"name": "db", "scope": "user", "host": "claude",
            "reason": "user-scope definition is never copied into a workspace file"} \
        in rep["not_included"]
    assert "not included: db" in sp.render_compile(rep)
    install_gateway(resolve_workspace(str(ws)), "claude", apply=True)
    for f in (ws / ".ctx-surface").rglob("*.json"):
        text = f.read_text()
        assert "SECRET" not in text and "db-mcp" not in text, f


# ================================================================ 3 signals
_FAKE_SLEEPER = """#!/bin/sh
if [ "$1" = "--help" ]; then echo "--settings"; exit 0; fi
echo started > {ready}
trap 'kill $SLEEPER 2>/dev/null; exit 42' TERM HUP
sleep 30 &
SLEEPER=$!
wait
"""


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_signal_cleans_temp_files_and_keeps_child_status(ws, tmp_path, sig):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ready = tmp_path / "ready"
    fake = bindir / "claude"
    fake.write_text(_FAKE_SLEEPER.format(ready=ready))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "fs-mcp"}}}))
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.local.json").write_text('{"enableAllProjectMcpServers": true}')
    rc_file = tmp_path / "rc"
    script = (f"import sys; sys.path.insert(0, {str(SRC)!r})\n"
              "from pathlib import Path\nfrom ctx.wrap import wrap_claude\n"
              f"rc = wrap_claude(Path({str(ws)!r}), ['-p', 'x'], '/opt/bin/ctx', surface='full')\n"
              f"Path({str(rc_file)!r}).write_text(str(rc))\n")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
           "TMPDIR": str(tmpdir)}
    proc = subprocess.Popen([sys.executable, "-c", script], env=env)
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    assert list(tmpdir.glob("ctx-surface-*")), "profile files staged"
    proc.send_signal(getattr(signal, sig))
    proc.wait(timeout=30)
    assert rc_file.read_text() == "42"                      # the child's own status
    assert list(tmpdir.iterdir()) == []                      # nothing left behind


# ================================================================ 4 evidence
def _pdir(ws: Path) -> Path:
    d = surface.claude_project_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    return d


_EVENT = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "b", "name": "Bash", "input": {}}]}}


def _skill(ws: Path, name: str) -> None:
    d = ws / ".claude" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Does {name}.\n---\nbody")
    os.utime(d / "SKILL.md", (1_000_000_000, 1_000_000_000))


def test_empty_or_unknown_files_are_not_sessions(ws):
    _skill(ws, "used")
    d = _pdir(ws)
    for i in range(3):
        (d / f"e{i}.jsonl").write_text("")
        (d / f"u{i}.jsonl").write_text('{"kind": "something-else"}\n')
    u = surface.transcript_usage(ws)
    assert u["sessions"] == 0 and u["unrecognized"] == 6
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert plan.settings == {}
    assert plan.receipt["skills"]["evidence"]["unrecognized_files"] == 6


def test_subagent_transcripts_are_read(ws):
    d = _pdir(ws)
    (d / "s1.jsonl").write_text(json.dumps(_EVENT) + "\n")
    sub = d / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "s", "name": "Skill", "input": {"skill": "deep"}}]}}) + "\n")
    u = surface.transcript_usage(ws)
    assert u["skills"] == {"deep": 1} and u["sessions"] == 1 and u["subagent_files"] == 1


def test_partial_scan_is_declared(ws, monkeypatch):
    _skill(ws, "unused")
    d = _pdir(ws)
    for i in range(8):
        f = d / f"s{i}.jsonl"
        f.write_text((json.dumps(_EVENT) + "\n") * 50)
        os.utime(f, (2_000_000_000 + i, 2_000_000_000 + i))
    real = surface.transcript_usage
    monkeypatch.setattr(surface, "transcript_usage",
                        lambda root, env=None: real(root, env=env, max_files=6))
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    item = plan.receipt["skills"]["items"][0]
    assert plan.receipt["skills"]["evidence"]["partial"] is True
    assert "partial evidence" in item["reason"] and "[partial evidence]" in plan.line


# ================================================================ 5 numbers
def test_text_audit_and_compile_name_their_basis(ws):
    from ctx import surface_profiles as sp

    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "fs-mcp"}}}))
    txt = surface.render_audit(surface.audit(ws))
    assert "inline basis" in txt and "deferred" in txt and "tok/turn" not in txt
    rep = sp.compile_profile(ws, "read-only", host="claude")
    assert rep["schema"] == "ctx.surface-compile/v2"
    assert rep["tokens"]["basis"] == "deferred" and "inline" in rep["tokens"]
    assert "deferred basis" in sp.render_compile(rep)


def test_always_load_server_is_not_priced_as_deferred(ws, tmp_path):
    script = tmp_path / "m.py"
    script.write_text(_MARKER_MCP)
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"pin": {
        "command": sys.executable, "args": [str(script), str(tmp_path / "mk")],
        "alwaysLoad": True}}}))
    _approve(ws)
    tool = next(r for r in surface.audit(ws, probe_mcp=True)["records"] if r["kind"] == "mcp_tool")
    assert tool["upfront_deferred"] == -1      # same as tokens: not deferred


# ================================================================ 6 server set
def test_dedupe_is_per_host(ws):
    (ws / ".cursor").mkdir()
    (ws / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "remote1": {"url": "https://cursor.example/mcp"}}}))
    _user_servers({"remote1": {"type": "http", "url": "https://claude.example/mcp"}})
    specs = [s for s in surface.mcp_server_specs(ws) if s.name == "remote1"]
    assert {s.host for s in specs} == {"cursor", "claude"}


@pytest.mark.parametrize("settings,started", [
    ({}, False),
    ({"enabledMcpjsonServers": ["fs"]}, True),
    ({"enableAllProjectMcpServers": True, "disabledMcpjsonServers": ["fs"]}, False),
])
def test_profile_never_starts_unapproved_project_server(ws, tmp_path, settings, started):
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "fs-mcp"}}}))
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text(json.dumps(settings))
    plan = sl.plan_claude_launch(ws, [], mode="full", child_env={},
                                 parent_env=dict(os.environ), tmpdir=tmp_path)
    mcp = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]
    assert ("fs" in mcp) is started
    row = plan.receipt["mcp"]["servers"][0]
    assert (row["mechanism"] == "not-started") is (not started)


def test_repository_config_cannot_select_a_profile(ws):
    (ws / "ctx.toml").write_text('version = 1\n[surface]\nwrap = "full"\n')
    mode, note = sl.resolve_mode(None, ws)
    assert mode == "auto" and "command line" in note
    assert sl.resolve_mode("full", ws) == ("full", "")
    (ws / "ctx.toml").write_text('version = 1\n[surface]\nwrap = "off"\n')
    assert sl.resolve_mode(None, ws) == ("off", "")


# ================================================================ 7/8 gateway
_DOT_MCP = (
    "import sys,json\n"
    "for line in sys.stdin:\n"
    " m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
    " if meth=='initialize': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{}}}),flush=True)\n"
    " elif meth=='tools/list': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[{'name':'tool_1','description':'d','inputSchema':{'type':'object'}}]}}),flush=True)\n"
    " elif meth=='tools/call': print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'content':[{'type':'text','text':'ok'}]}}),flush=True)\n"
)


def test_dotted_server_name_reveals_and_routes(ws, tmp_path):
    script = tmp_path / "d.py"
    script.write_text(_DOT_MCP)
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"my.srv": {
        "command": sys.executable, "args": [str(script)]}}}))
    g = gw.Gateway(ws, persist=False)
    res, changed = g.call("surface_reveal", {"target": "mcp__my.srv__tool_1"})
    assert changed and "mcp__my.srv__tool_1" in res["content"][0]["text"]
    assert "mcp__my.srv__tool_1" in {t["name"] for t in g.visible_tools()}
    assert g.call("mcp__my.srv__tool_1", {})[0]["content"][0]["text"] == "ok"
    g.close()


def test_query_answers_from_probe_cache_without_starting_backends(ws, tmp_path):
    marker = tmp_path / "spawned"
    argv = [sys.executable, "-c", f"open({str(marker)!r},'w').write('x')"]
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"cold": {
        "command": argv[0], "args": argv[1:]}}}))
    (ws / ".ctx-surface").mkdir()
    (ws / ".ctx-surface" / "probe-cache.json").write_text(json.dumps({"cold": {
        "key": surface._probe_cache_key(argv),
        "tools": [{"name": "drop_table", "summary": "Drop a database table", "tokens": 50}]}}))
    g = gw.Gateway(ws, persist=False)
    text = g.call("surface_index", {"query": "table"})[0]["content"][0]["text"]
    assert "mcp__cold__drop_table" in text and "Drop a database table" in text
    assert not marker.exists() and g._backends == {}
    g.close()


# ================================================================ 9 smaller
@pytest.mark.parametrize("args", [["--surface"], ["--surface="], ["--surface", "auto", "--surface", "off"],
                                  ["--surface", "--", "-p", "x"]])
def test_bad_surface_flag_is_refused_not_passed_through(ws, monkeypatch, capsys, args):
    from argparse import Namespace

    import ctx.wrap as wrap_mod
    from ctx.commands.hosts import cmd_wrap

    calls = []
    monkeypatch.setattr(wrap_mod, "wrap_claude", lambda *a, **k: calls.append(a) or 0)
    monkeypatch.chdir(ws)
    ns = Namespace(host="claude", agent_args=list(args), print_config=False, workspace=str(ws))
    assert cmd_wrap(ns) == 2 and calls == []
    assert "--surface takes exactly one value" in capsys.readouterr().err


def test_fifo_and_oversized_configs_are_refused(ws):
    os.mkfifo(_home() / ".claude.json")
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"big": {
        "command": "x", "args": ["y" * (surface.MAX_READ_BYTES + 10)]}}}))
    done: list = []
    t = threading.Thread(target=lambda: done.append(surface.mcp_server_specs(ws)), daemon=True)
    t.start()
    t.join(10)
    assert done == [[]]


def test_receipt_write_is_atomic(ws):
    assert sl.write_receipt(ws, {"schema": sl.WRAP_SCHEMA}) == sl.RECEIPT_FILE
    files = sorted(p.name for p in (ws / ".ctx-surface").iterdir())
    assert files == ["wrap-receipt.json"]


# ================================================================ round 3
_CANARY_ARGS = ["-e", "API_KEY=DOCKERCANARY_xyz", "--env", "TOK=ENVCANARY_v",
                "https://api.example.com/v1/PATHCANARY_tok123/events",
                "--config", '{"auth":{"token":"JSONCANARY_abc"}}',
                "--name", "INNOCCANARY_value123", "POSITIONALCANARY_pw",
                "postgres://u:USERINFOCANARY@db/x"]


def _run_ctx(ws: Path, *args: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.run([sys.executable, "-m", "ctx", "--workspace", str(ws), *args],
                          capture_output=True, text=True, env=env, timeout=120)
    return proc.stdout + proc.stderr


def test_no_argument_value_reaches_any_rendering(ws, tmp_path):
    from ctx import surface_profiles as sp

    _user_servers({"dock": {"command": "/usr/bin/docker", "args": _CANARY_ARGS}})
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "wsdock": {"command": "docker", "args": _CANARY_ARGS}}}))
    _approve(ws)
    out = "".join(_run_ctx(ws, "surface", cmd, *extra) for cmd, extra in (
        ("audit", ["--json"]), ("audit", []), ("inventory", ["--json"]), ("inventory", []),
        ("trim", []), ("graph", []), ("explain", ["mcp.dock"]),
        ("compile", ["--profile", "full"]), ("compile", ["--profile", "full", "--json"])))
    out += _run_ctx(ws, "prune", "--json") + _run_ctx(ws, "prune", "--yes")
    out += repr(surface.mcp_server_specs(ws))
    assert "mcp.dock" in out and "CANARY" not in out
    rec = {r.id: r for r in surface.collect_surface(ws)}["mcp.dock"]
    assert rec.detail.endswith("docker (11 args; flags -e --env --config --name)")
    # the workspace launch file carries the real command — at 0600
    rep = sp.compile_profile(ws, "full", host="claude", apply=True)
    f = ws / ".ctx-surface" / "mcp.claude.json"
    assert "DOCKERCANARY" in f.read_text()                  # it is the launch file
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert rep["written"]


def test_install_gateway_files_are_private(ws):
    from ctx.installer import install_gateway
    from ctx.workspace import resolve_workspace

    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"w": {"command": "w", "args": ["-e", "K=v"]}}}))
    install_gateway(resolve_workspace(str(ws)), "claude", apply=True)
    for f in (ws / ".ctx-surface").glob("*.json"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_large_user_config_still_read_and_skips_are_declared(ws, monkeypatch):
    big = {"mcpServers": {"u": {"command": "u"}}, "history": ["x" * 1000] * 5200}  # ~5 MiB
    (_home() / ".claude.json").write_text(json.dumps(big))
    assert "u" in {s.name for s in surface.mcp_server_specs(ws)}
    monkeypatch.setattr(surface, "MAX_READ_BYTES", 1024)
    (ws / ".mcp.json").write_text("{not json")
    surface.mcp_server_specs(ws)
    gaps = surface.discovery_gaps(ws)
    assert any(".claude.json" in g and "larger than" in g for g in gaps)
    assert any(".mcp.json" in g and "invalid JSON" in g for g in gaps)
    a = surface.audit(ws)
    assert any("config skipped" in g for g in a["unresolved_sources"])
    assert "Not counted: config skipped" in surface.render_audit(a)
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    assert any("config skipped" in g for g in plan.receipt["unresolved_sources"])
    assert "config file(s) skipped" in plan.line


def test_failed_probe_is_declared_everywhere(ws, tmp_path, monkeypatch):
    script = tmp_path / "fail.py"
    script.write_text(_FAILING)
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"bad": {
        "command": sys.executable, "args": [str(script), str(tmp_path / "c")]}}}))
    _approve(ws)
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    adv = surface.preflight(ws, max_static_tokens=1)
    assert "probe failed (cost shown is a floor): bad" in adv and "--probe-mcp" in adv
    a = surface.audit(ws)
    assert "bad" in a["probe_failed"]
    rec = next(r for r in a["records"] if r["id"] == "mcp.bad")
    assert rec["pricing"] == "probe-failed (floor)" and "floor" in rec["detail"]
    assert "Probe failed: bad" in surface.render_audit(a)
    plan = sl.plan_claude_launch(ws, [], mode="auto", child_env={}, parent_env=dict(os.environ))
    row = plan.receipt["mcp"]["servers"][0]
    assert row["probe_failed"] and "floor" in row["reason"]
    assert "probe failed: bad" in plan.line


def test_compile_and_prune_state_what_the_launch_line_drops(ws):
    from ctx import surface_profiles as sp
    from ctx.prune import render_prune, run_prune

    _user_servers({"usr": {"command": "usr-mcp"}})
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "fs": {"command": "fs-mcp"}, "web": {"type": "http", "url": "https://w.example/mcp"}}}))
    _approve(ws)
    rep = sp.compile_profile(ws, "full", host="claude")
    txt = sp.render_compile(rep)
    assert "remote servers: kept web" in txt
    launch_at = txt.index("launch:")
    assert "WILL NOT have" in txt[launch_at:] and "usr — user-scope" in txt[launch_at:]
    assert json.loads(rep["files"][".ctx-surface/mcp.claude.json"])["mcpServers"]["web"]["url"]
    prep = run_prune(ws, hosts=("claude",), apply=False)
    ptxt = render_prune(prep)
    assert "not included 1" in ptxt and "usr — user-scope" in ptxt


def test_audit_totals_exclude_unapproved_project_servers(ws, tmp_path):
    script = tmp_path / "m.py"
    script.write_text(_MARKER_MCP)
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"p": {
        "command": sys.executable, "args": [str(script), str(tmp_path / "mk")]}}}))
    a = surface.audit(ws, probe_mcp=True)
    recs = [r for r in a["records"] if r["provider"] == "p"]
    assert recs and all(r["tokens"] == 0 and r["activation"] == "not-started" for r in recs)
    _approve(ws)
    assert sum(r["tokens"] for r in surface.audit(ws, probe_mcp=True)["records"]
               if r["provider"] == "p") > 0


@pytest.mark.parametrize("flavor,basis", [("codex", "Codex"), ("antigravity", "Antigravity")])
def test_session_start_prices_with_the_hook_flavor_host(ws, monkeypatch, capsys, flavor, basis):
    import io

    from ctx import hook

    (ws / "ctx.toml").write_text("version = 1\n[surface]\nmax_static_tokens = 1\n")
    (ws / "AGENTS.md").write_text("Instructions. " * 50)
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")   # Claude's rule would say inline
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(ws)})))
    monkeypatch.chdir(ws)
    hook.main_session_start(flavor)
    assert f"deferred basis: {basis}" in capsys.readouterr().out
