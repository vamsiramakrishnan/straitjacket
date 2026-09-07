"""Real stdio protocol exchanges; no model calls or provider credentials."""
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from ctx.acp import CONFIG, DEFAULT_COMMANDS, Endpoint, configure, launch, settings


@pytest.fixture
def agent(tmp_path):
    path = tmp_path / "agent.py"
    path.write_text('''
import json, os, sys, time, subprocess
mode = os.environ.get("ACP_TEST_MODE", "ok")
def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)
for line in sys.stdin:
    p = json.loads(line)
    with open(os.environ["ACP_TEST_LOG"], "a") as f:
        f.write(line)
    method = p.get("method")
    if method == "initialize":
        if mode == "bad":
            print("not JSON", flush=True)
            continue
        if mode == "huge":
            print("x" * (3 * 1024 * 1024), flush=True)
            continue
        if mode == "exit":
            sys.exit(1)
        send({"id":p["id"], "result":{"protocolVersion":2 if mode == "version" else 1}})
    elif method == "session/new":
        if mode == "mcp":
            server = p["params"]["mcpServers"][0]
            request = {"jsonrpc":"2.0", "id":1, "method":"tools/list"}
            proc = subprocess.run([server["command"], *server["args"]],
                input=json.dumps(request) + "\\n", text=True, capture_output=True, timeout=10)
            assert proc.returncode == 0, proc.stderr
            names = [tool["name"] for tool in json.loads(proc.stdout)["result"]["tools"]]
            assert names == ["ctx", "ctx_edit"], names
        send({"id":p["id"], "result":{"sessionId":"s", "models":{
            "currentModelId":"test-model", "availableModels":[{"modelId":"test-model"}]}}})
    elif method == "session/prompt":
        if mode == "timeout":
            time.sleep(30)
        if mode in ("permission", "allow"):
            send({"id":99, "method":"session/request_permission", "params":{
                "sessionId":"s", "options":[{"kind":"allow_once", "optionId":"a"},
                                              {"kind":"reject_once", "optionId":"r"}]}})
            reply = json.loads(sys.stdin.readline())
            with open(os.environ["ACP_TEST_LOG"], "a") as f:
                f.write(json.dumps(reply) + "\\n")
            assert reply["result"]["outcome"]["optionId"] == ("a" if mode == "allow" else "r")
        send({"id":98, "method":"fs/write_text_file", "params":{"sessionId":"s"}})
        assert json.loads(sys.stdin.readline())["error"]["code"] == -32601
        send({"method":"session/update", "params":{"sessionId":"other", "update":{
            "sessionUpdate":"agent_message_chunk", "content":{"type":"text", "text":"WRONG SESSION"}}}})
        for text in ("work ", "done"):
            send({"method":"session/update", "params":{"sessionId":"s", "update":{
                "sessionUpdate":"agent_message_chunk", "content":{"type":"text", "text":text}}}})
        send({"id":p["id"], "result":{"stopReason":"cancelled" if mode == "cancelled" else "end_turn"}})
    elif method == "session/cancel":
        sys.exit(0)
''')
    return path


def run_agent(agent, mode="ok", **kwargs):
    endpoint = Endpoint((sys.executable, str(agent)), "test-model",
                        permissions="allow_once" if mode == "allow" else "deny")
    env = {**os.environ, "ACP_TEST_MODE": mode, "ACP_TEST_LOG": str(agent.parent / "wire.jsonl")}
    return launch(endpoint, agent.parent, "do work", shlex.join([sys.executable, "-m", "ctx"]),
                  timeout=5, env=env, **kwargs)


@pytest.mark.parametrize("host", DEFAULT_COMMANDS)
def test_setup_detection_and_worker_transport(host, agent):
    configure(agent.parent, host, "test-model", command=[sys.executable, str(agent)])
    from ctx.hosts import detect_all
    from ctx.orchestrator import _launch_host
    detected = next(h for h in detect_all(workspace_root=agent.parent) if h.name == host)
    assert detected.installed and detected.spec.unattended and detected.acp
    assert detected.models[0].id == "test-model"
    os.environ["ACP_TEST_LOG"] = str(agent.parent / "wire.jsonl")
    try:
        result = _launch_host(detected, agent.parent, "task", shlex.join([sys.executable, "-m", "ctx"]), timeout=5)
    finally:
        os.environ.pop("ACP_TEST_LOG", None)
    assert result[:2] == (0, "work done"), result
    assert result[3] is None  # never fabricate usage
    wire = [json.loads(l) for l in (agent.parent / "wire.jsonl").read_text().splitlines()]
    new = next(p for p in wire if p.get("method") == "session/new")
    assert new["params"]["cwd"] == str(agent.parent)
    assert new["params"]["mcpServers"][0]["args"][-2:] == ["--workspace", str(agent.parent)]
    assert not wire[0]["params"]["clientCapabilities"]


@pytest.mark.parametrize("mode,reason", [("bad", "Expecting value"), ("huge", "frame exceeds"),
    ("exit", "closed stdout"), ("version", "negotiate"), ("cancelled", "cancelled"),
    ("permission", "unresolved permission")])
def test_failed_protocol_never_succeeds(agent, mode, reason):
    code, _, error, usage = run_agent(agent, mode)
    assert code != 0 and reason in error
    assert usage is None


def test_explicit_allow_once(agent):
    assert run_agent(agent, "allow")[:2] == (0, "work done")


def test_agent_can_launch_injected_mcp_server(agent):
    assert run_agent(agent, "mcp")[:2] == (0, "work done")


def test_idle_timeout_cleans_up(agent):
    import time
    start = time.monotonic()
    result = run_agent(agent, "timeout", idle_timeout=.3)
    assert result[0] and "idle timeout" in result[2]
    assert time.monotonic() - start < 3


def test_bad_configuration_is_not_overwritten(agent):
    path = agent.parent / CONFIG
    path.parent.mkdir()
    path.write_text("broken")
    with pytest.raises(ValueError, match="Invalid"):
        configure(agent.parent, "hermes", "test-model", command=[sys.executable])
    assert path.read_text() == "broken"


def test_unsupported_model_refused_before_prompt(agent):
    endpoint = Endpoint((sys.executable, str(agent)), "missing-model")
    log = agent.parent / "wire.jsonl"
    result = launch(endpoint, agent.parent, "task", sys.executable, timeout=3,
                    env={**os.environ, "ACP_TEST_LOG": str(log)})
    assert result[0] and "does not advertise" in result[2]
    assert '"method": "session/prompt"' not in log.read_text()
