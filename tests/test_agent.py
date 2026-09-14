"""`ctx agent`: ctx as the host on the Claude Agent SDK. The SDK is an
extra, so these tests cover what does not need it (the first turn, the
result shape, the CLI's refusal) and skip the tool surface when it is
absent."""

from __future__ import annotations

import json
import sys
import types

import pytest

from conftest import make_store, make_ws

HAS_SDK = True
try:
    import claude_agent_sdk  # noqa: F401
except Exception:
    HAS_SDK = False


@pytest.fixture()
def repo(workspace_dir):
    (workspace_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return workspace_dir


def test_first_turn_carries_the_pack_and_falls_back_without_it(state_home, repo):
    from ctx.agent import _first_turn

    ws = make_ws(repo)
    store = make_store(ws)
    prompt, info = _first_turn(ws, store, "extend the add function in calc.py to handle floats", pack=True, budget=800)
    assert prompt.startswith("extend the add function in calc.py to handle floats") and "<context-pack>" in prompt
    assert info and info["files"] == ["calc.py"] and info["tokens"] > 0
    prompt, info = _first_turn(ws, store, "extend the add function in calc.py to handle floats", pack=False, budget=800)
    assert prompt == "extend the add function in calc.py to handle floats" and info is None


def test_result_json_is_the_hosts_shape():
    from ctx.agent import _result_json

    msg = types.SimpleNamespace(
        subtype="success", is_error=False, num_turns=3, duration_ms=1200, duration_api_ms=900,
        total_cost_usd=0.0123, usage={"input_tokens": 5, "cache_read_input_tokens": 900},
        model_usage={"claude-haiku-4-5": {"input_tokens": 5, "cost_usd": 0.0123}},
        session_id="s1", result="done", stop_reason="end_turn", errors=[],
    )
    doc = _result_json(msg, pack_info={"files": ["a.py"], "tokens": 40}, wall=2.5)
    assert doc["type"] == "result" and doc["num_turns"] == 3 and doc["total_cost_usd"] == 0.0123
    assert doc["usage"]["cache_read_input_tokens"] == 900
    assert list(doc["modelUsage"]) == ["claude-haiku-4-5"]
    assert doc["pack"]["files"] == ["a.py"] and doc["runtime"] == "ctx agent"
    json.dumps(doc)  # serialisable as the harness reads it


@pytest.mark.skipif(HAS_SDK, reason="the SDK is installed here")
def test_cli_agent_without_the_sdk_says_how_to_get_it(state_home, repo, capsys):
    from ctx.cli import main

    rc = main(["--workspace", str(repo), "agent", "-p", "hello", "--output-format", "json"])
    err = capsys.readouterr().err
    assert rc == 2 and "claude-agent-sdk" not in err.lower() or "ctx-harness[agent]" in err


@pytest.mark.skipif(not HAS_SDK, reason="claude-agent-sdk not installed ([agent] extra)")
def test_tool_surface_answers_in_process(state_home, repo):
    import asyncio

    from ctx.agent import BUILTIN_TOOLS, _tools

    ws = make_ws(repo)
    store = make_store(ws)
    tools = {t.name: t for t in _tools(ws, store)}
    assert set(tools) == {"search", "outline", "get", "refs", "pack"}
    assert "Grep" not in BUILTIN_TOOLS and "Glob" not in BUILTIN_TOOLS

    out = asyncio.run(tools["outline"].handler({"path": "calc.py"}))
    assert "add" in out["content"][0]["text"]
    out = asyncio.run(tools["search"].handler({"query": "def add file:calc"}))
    assert "calc.py:" in out["content"][0]["text"]
    out = asyncio.run(tools["get"].handler({"path": "calc.py", "symbol": "add"}))
    assert "return a + b" in out["content"][0]["text"]
    out = asyncio.run(tools["search"].handler({"query": ""}))
    assert "give a pattern" in out["content"][0]["text"]


def test_agent_module_imports_without_the_sdk(monkeypatch):
    """The CLI must load the module (help, dispatch) on a machine without
    the extra; only running a session needs it."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    import importlib

    import ctx.agent as agent

    importlib.reload(agent)
    with pytest.raises(agent.AgentUnavailable):
        agent._sdk()


def test_shell_grep_is_translated_to_the_search_tool():
    from ctx.agent import _search_equivalent

    assert _search_equivalent('grep -rn "taint" bandit/core') == 'search {"query": "taint file:bandit/core"}'
    assert _search_equivalent("grep -i source bandit/ tests/") == 'search {"query": "source case:no file:bandit file:tests"}'
    assert _search_equivalent("cd /x && rg -n 'class Foo' src | head") == 'search {"query": "\'class Foo\' file:src"}'
    assert _search_equivalent("git grep -e request.args -- bandit") == 'search {"query": "request.args file:bandit"}'
    assert _search_equivalent("grep -rn --include=*.py execute .") == 'search {"query": "execute file:*.py"}'
    assert _search_equivalent("ls bandit/plugins") is None
    assert _search_equivalent("python -m pytest -q") is None
    assert _search_equivalent("grep") is None
    assert _search_equivalent("echo 'unbalanced") is None


def test_bash_router_denies_grep_and_passes_everything_else():
    import asyncio

    from ctx.agent import _route_bash

    out = asyncio.run(_route_bash({"tool_input": {"command": "grep -rn foo src"}}, "t1", None))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and 'search {"query": "foo file:src"}' in hso["permissionDecisionReason"]
    assert asyncio.run(_route_bash({"tool_input": {"command": "pytest -q"}}, "t2", None)) == {}
    assert asyncio.run(_route_bash({"tool_input": {}}, "t3", None)) == {}
