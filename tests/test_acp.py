"""Real stdio protocol exchanges; no model calls or provider credentials."""
import json
import os
import shlex
import sys
import time
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
        if mode == "relay":
            os.environ["ACP_MCP_SERVER"] = json.dumps(p["params"]["mcpServers"][0])
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
        if mode == "slow":
            # Long enough that a relay interrupt lands mid-turn, and reading
            # stdin between chunks so a protocol cancel is observed rather
            # than only the kill that follows it.
            import select
            for _ in range(400):
                send({"method":"session/update", "params":{"sessionId":"s", "update":{
                    "sessionUpdate":"agent_message_chunk", "content":{"type":"text", "text":"."}}}})
                if select.select([sys.stdin], [], [], .05)[0]:
                    frame = sys.stdin.readline()
                    with open(os.environ["ACP_TEST_LOG"], "a") as f:
                        f.write(frame)
                    if json.loads(frame).get("method") == "session/cancel":
                        sys.exit(0)
        if mode == "echo":
            with open(os.environ["ACP_TEST_LOG"], "a") as f:
                f.write(json.dumps({"prompt": p["params"]["prompt"][0]["text"]}) + "\\n")
        if mode == "relay":
            # Drive the injected MCP server the way an ACP worker would: one
            # process, several tools/call frames, reading the relay it was
            # never given a hook to reach.
            server = json.loads(os.environ["ACP_MCP_SERVER"])
            def call(op, options):
                return {"jsonrpc":"2.0", "id":op, "method":"tools/call",
                        "params":{"name":"ctx", "arguments":{"op":op, "options":options}}}
            frames = [
                {"jsonrpc":"2.0", "id":0, "method":"initialize", "params":{}},
                call("relay_watch", {"subscriber":"codex", "topic":"job"}),
                call("relay_publish", {"topic":"peer", "to":"claude",
                                       "ref":"repo:a.py", "note":"from an acp worker"}),
                call("relay_pending", {"subscriber":"claude"}),
            ]
            proc = subprocess.run([server["command"], *server["args"]],
                input="".join(json.dumps(f) + "\\n" for f in frames),
                text=True, capture_output=True, timeout=20)
            assert proc.returncode == 0, proc.stderr
            out = {}
            for line in proc.stdout.splitlines():
                v = json.loads(line)
                if v.get("id") in ("relay_watch", "relay_publish", "relay_pending"):
                    out[v["id"]] = v["result"]["content"][0]["text"]
            assert "watching job" in out["relay_watch"], out
            assert "queued advise for claude" in out["relay_publish"], out
            assert "repo:a.py" in out["relay_pending"], out
            assert "from an acp worker" in out["relay_pending"], out
            with open(os.environ["ACP_TEST_LOG"], "a") as f:
                f.write(json.dumps({"relay": out}) + "\\n")
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
    kwargs.setdefault("timeout", 5)
    return launch(endpoint, agent.parent, "do work", shlex.join([sys.executable, "-m", "ctx"]),
                  env=env, **kwargs)


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


def test_an_acp_worker_can_drive_the_relay_through_its_injected_mcp_server(agent):
    """The relay has to be reachable from an ACP worker, not only from a
    hooked host. An ACP worker gets no PreToolUse and no PostToolUse of ours
    — the session-scoped MCP server is its whole ctx surface, so the relay
    ops have to live on it or the transport cannot collaborate at all."""
    (agent.parent / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    assert run_agent(agent, "relay")[:2] == (0, "work done")

    from ctx import relay
    signals = relay.pending(agent.parent, "claude")
    assert [s["ref"] for s in signals] == ["repo:a.py"]
    assert signals[0]["note"] == "from an acp worker"
    assert [w["subscriber"] for w in relay.active_watches(agent.parent)] == ["codex"]


def test_a_queued_report_reaches_an_acp_worker_through_its_prompt(agent):
    """An ACP worker is not a hooked host: no PreToolUse, no PostToolUse, so
    no additionalContext. The prompt is the only delivery point it has."""
    from ctx import relay

    (agent.parent / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    relay.publish(agent.parent, topic="job", to="codex", origin="ctx-job",
                  ref="run:8f2c1d3e4a5b#stdout", note="pytest: 3 failed")
    assert run_agent(agent, "echo", relay_subscriber="codex")[0] == 0

    wire = [json.loads(l) for l in (agent.parent / "wire.jsonl").read_text().splitlines()]
    sent = next(v["prompt"] for v in wire if isinstance(v, dict) and "prompt" in v)
    assert "run:8f2c1d3e4a5b#stdout" in sent and "pytest: 3 failed" in sent
    assert sent.endswith("do work")           # the task itself still arrives last
    assert relay.pending(agent.parent, "codex") == []   # and is not re-delivered


def test_an_interrupt_queued_before_launch_stops_the_worker_at_once(agent):
    from ctx import relay

    (agent.parent / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    relay.interrupt(agent.parent, to="codex", ref="checkpoint:d914ee702801",
                    origin="claude", note="the schema changed under you")

    code, _, error, usage = run_agent(agent, "slow", relay_subscriber="codex")
    assert code != 0 and usage is None
    assert "relay interrupt" in error
    assert "checkpoint:d914ee702801" in error
    assert "the schema changed under you" in error
    # Drained, so the next attempt is not killed by a message already acted on.
    assert relay.pending(agent.parent, "codex", kinds=("interrupt",)) == []


def test_a_relay_interrupt_stops_an_acp_worker_mid_turn(agent):
    """The one transport where a real mid-turn stop is honest.

    ctx owns this subprocess and already sends session/cancel on teardown; a
    PreToolUse hook cannot see assistant tokens and so can only stop the next
    tool call. Here the interrupt is published *after* the turn is under way
    and the worker is cut off in the middle of it.
    """
    import threading

    from ctx import relay

    (agent.parent / "ctx.toml").write_text("version = 1\n", encoding="utf-8")

    def interrupt_soon():
        time.sleep(1.5)
        relay.interrupt(agent.parent, to="codex", ref="checkpoint:d914ee702801",
                        origin="claude", note="stop, the schema moved")

    thread = threading.Thread(target=interrupt_soon, daemon=True)
    thread.start()
    try:
        code, partial, error, _ = run_agent(agent, "slow", relay_subscriber="codex", timeout=20)
    finally:
        thread.join(timeout=5)

    assert code != 0
    assert "relay interrupt" in error and "checkpoint:d914ee702801" in error
    # Not a wall timeout dressed up as a stop: the reason is the relay's.
    assert "timeout" not in error

    wire = (agent.parent / "wire.jsonl").read_text()
    assert "session/prompt" in wire, "the turn never started, so nothing was interrupted"
    assert partial, "the worker produced nothing, so the turn was not under way"
    assert relay.pending(agent.parent, "codex", kinds=("interrupt",)) == []

    # Deliberately not asserted: that the agent *logged* receiving
    # session/cancel. The transport sends that frame and then reaps the
    # process immediately, so whether a busy agent is scheduled in time to
    # read it is a race. Asserting it would buy a flaky test, not a stronger
    # claim — what matters here is that the worker stopped mid-turn for the
    # relay's reason, which the assertions above pin down.


def test_a_worker_with_no_relay_address_is_untouched(agent):
    """Opt-in: the semantic worker runs with no tools over frozen evidence and
    must stay unreachable even while interrupts are queued for its host."""
    from ctx import relay

    (agent.parent / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    relay.interrupt(agent.parent, to="codex", ref="repo:a.py", origin="claude")
    assert run_agent(agent, "ok")[:2] == (0, "work done")
    assert relay.pending(agent.parent, "codex", kinds=("interrupt",))


def test_the_relay_never_removes_the_callers_own_cancellation(agent):
    from ctx import relay

    (agent.parent / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    code, _, error, _ = run_agent(agent, "slow", relay_subscriber="codex",
                                  cancelled=lambda: True)
    assert code != 0 and "cancelled" in error


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
