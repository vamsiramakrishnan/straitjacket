"""Tests for generic host/plugin extensibility hooks: private ledger overrides,
unknown-command defaults, outside read prefixes, passthrough filenames,
single-underscore and call_mcp_tool self-guards, Antigravity overwriteResult,
PreInvocation step format, and MCP _meta workspace resolution."""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from ctx import hook, mcp, sessiondir


def _run_hook(monkeypatch, fn, flavor: str, payload: dict) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = fn(flavor)
    assert rc == 0
    return json.loads(buf.getvalue())


def test_sessiondir_private_ledgers_all_and_prefixes(tmp_path, monkeypatch):
    state = tmp_path / "state"
    ws = tmp_path / "ro_mount" / "repo"
    ws.mkdir(parents=True)
    monkeypatch.setenv("CTX_STATE_HOME", str(state))

    # Default stays inside workspace for non-private ledgers.
    monkeypatch.delenv("CTX_PRIVATE_LEDGERS_ALL", raising=False)
    monkeypatch.delenv("CTX_PRIVATE_LEDGER_PREFIXES", raising=False)
    assert sessiondir.session_reads_dir(ws, "engagement.json").startswith(str(ws))

    # CTX_PRIVATE_LEDGERS_ALL=1 redirects every ledger to state dir.
    monkeypatch.setenv("CTX_PRIVATE_LEDGERS_ALL", "1")
    priv = sessiondir.session_reads_dir(ws, "engagement.json")
    assert priv.startswith(str(state))
    assert not priv.startswith(str(ws))

    # CTX_PRIVATE_LEDGER_PREFIXES matches only configured prefix.
    monkeypatch.delenv("CTX_PRIVATE_LEDGERS_ALL", raising=False)
    other = tmp_path / "local" / "repo"
    other.mkdir(parents=True)
    monkeypatch.setenv("CTX_PRIVATE_LEDGER_PREFIXES", str(tmp_path / "ro_mount"))
    assert sessiondir.session_reads_dir(ws, "engagement.json").startswith(str(state))
    assert sessiondir.session_reads_dir(other, "engagement.json").startswith(str(other))


def test_guard_unknown_command_env_and_toml_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_GUARD_UNKNOWN_COMMAND", "allow")
    pol = hook._load_guard_policy(str(tmp_path))
    assert pol["unknown_command"] == "allow"

    # Explicit ctx.toml overrides the environment default.
    (tmp_path / "ctx.toml").write_text('[guard]\nunknown_command = "deny"\n', encoding="utf-8")
    pol2 = hook._load_guard_policy(str(tmp_path))
    assert pol2["unknown_command"] == "deny"


def test_allow_outside_read_prefixes_preserves_secret_and_home_guards(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    docs = tmp_path / "shared" / "docs"
    docs.mkdir(parents=True)
    note = docs / "guide.md"
    note.write_text("# Guide\n", encoding="utf-8")
    secret = docs / ".env"
    secret.write_text("SECRET=1\n", encoding="utf-8")

    # Without env prefix: outside.
    monkeypatch.delenv("CTX_ALLOW_OUTSIDE_READ_PREFIXES", raising=False)
    assert hook._read_path_class(str(note), str(ws)) == "outside"

    # With env prefix: allowed, while secrets remain blocked.
    monkeypatch.setenv("CTX_ALLOW_OUTSIDE_READ_PREFIXES", str(docs))
    assert hook._read_path_class(str(note), str(ws)) is None
    assert hook._read_path_class(str(secret), str(ws)) == "secret"

    # Root `/` is never accepted as an outside-read prefix.
    monkeypatch.setenv("CTX_ALLOW_OUTSIDE_READ_PREFIXES", os.sep)
    assert hook._read_path_class(str(note), str(ws)) == "outside"


def test_passthrough_filenames_skip_read_slicing_and_output_truncation(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    skill = ws / "SKILL.md"
    body = "# Skill\n\n## Rules\n\n" + ("- Always follow rule line.\n" * 1000)
    skill.write_text(body, encoding="utf-8")

    monkeypatch.setenv("CTX_PASSTHROUGH_FILENAMES", "SKILL.md,AGENTS.md")
    monkeypatch.setenv("CTX_ANTIGRAVITY_OVERWRITE_RESULT", "1")
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))

    pre = _run_hook(
        monkeypatch,
        hook.main_pre_tool_use,
        "antigravity",
        {
            "toolName": "read_file",
            "toolInput": {"Path": str(skill)},
            "workspacePaths": [str(ws)],
        },
    )
    assert pre == {"decision": "allow"}

    post = _run_hook(
        monkeypatch,
        hook.main_post_tool_use,
        "antigravity",
        {
            "toolName": "read_file",
            "toolInput": {"Path": str(skill)},
            "result": body,
            "workspacePaths": [str(ws)],
        },
    )
    assert post == {}


def test_whole_file_markdown_read_returns_section_outline(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    doc = ws / "architecture.md"
    lines = ["# System Design\n"]
    for i in range(1, 20):
        lines.append(f"## Component {i}\n\n- Detail line for component {i} with `code`.\n" + "prose " * 180 + "\n")
    doc.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.delenv("CTX_PASSTHROUGH_FILENAMES", raising=False)

    pre = _run_hook(
        monkeypatch,
        hook.main_pre_tool_use,
        "antigravity",
        {
            "toolName": "read_file",
            "toolInput": {"Path": str(doc)},
            "workspacePaths": [str(ws)],
        },
    )
    assert pre["decision"] == "deny"
    assert "section outline" in pre["reason"]
    assert "Component 1" in pre["reason"]


def test_antigravity_overwrite_result_and_self_mcp_guards(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CTX_ANTIGRAVITY_OVERWRITE_RESULT", "1")
    monkeypatch.setenv("CTX_MCP_SELF_SERVERS", "custom_ctx_server")

    # Over-budget non-ctx tool output gets overwritten with markdown/v1 digest.
    big_md = (
        "# Quarterly Plan\n\n"
        + "\n".join(f"## Track {i}\n\n- Action item {i} with **owner**.\n" + "details " * 160 for i in range(1, 18))
    )
    post = _run_hook(
        monkeypatch,
        hook.main_post_tool_use,
        "antigravity",
        {
            "toolName": "run_command",
            "toolInput": {"CommandLine": "doccli read 123"},
            "result": big_md,
            "workspacePaths": [str(ws)],
        },
    )
    assert "overwriteResult" in post
    assert "profile=markdown/v1]" in post["overwriteResult"]

    # Self MCP calls via single-underscore or call_mcp_tool pass through untouched.
    for payload in (
        {"toolName": "mcp_custom_ctx_server_ctx", "result": big_md, "workspacePaths": [str(ws)]},
        {
            "toolName": "call_mcp_tool",
            "toolInput": {"ServerName": "custom_ctx_server", "ToolName": "ctx"},
            "result": big_md,
            "workspacePaths": [str(ws)],
        },
    ):
        assert _run_hook(monkeypatch, hook.main_post_tool_use, "antigravity", payload) == {}


def test_pre_invocation_system_message_and_rewrite_bin(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    custom_bin = tmp_path / "bin" / "ctx"
    custom_bin.parent.mkdir()
    custom_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    custom_bin.chmod(0o755)
    monkeypatch.setenv("CTX_REWRITE_BIN", str(custom_bin))
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))

    # PreToolUse (agy 1.5.4+ with hookEventName) rewrites leading `ctx ` to CTX_REWRITE_BIN.
    pre = _run_hook(
        monkeypatch,
        hook.main_pre_tool_use,
        "antigravity",
        {
            "hookEventName": "PreToolUse",
            "toolName": "run_command",
            "toolInput": {"CommandLine": "pytest -q"},
            "workspacePaths": [str(ws)],
        },
    )
    assert pre["decision"] == "allow"
    assert pre["overwrite"]["CommandLine"].startswith(str(custom_bin) + " run")

    # Legacy Antigravity payload (no hookEventName) degrades to deny with rewritten hint.
    pre_legacy = _run_hook(
        monkeypatch,
        hook.main_pre_tool_use,
        "antigravity",
        {
            "toolName": "run_command",
            "toolInput": {"CommandLine": "pytest -q"},
            "workspacePaths": [str(ws)],
        },
    )
    assert pre_legacy["decision"] == "deny"
    assert f"Re-run it as: {custom_bin} run" in pre_legacy["reason"]

    # Running custom_bin directly is recognized as a self ctx invocation, even
    # in embedded Python runtimes where sys.executable is None or empty.
    assert hook._is_ctx_invocation(f"{custom_bin} get run:abc123#stdout", str(ws))
    monkeypatch.setattr(sys, "executable", None)
    assert hook._is_ctx_invocation(f"{custom_bin} get run:abc123#stdout", str(ws))

    # PreInvocation emits hidden systemMessage when opted in.
    from ctx import relay

    relay.watch(str(ws), subscriber="antigravity", topic="digest")
    relay.publish(str(ws), topic="digest", selector="run", ref="run:abc123#stdout", origin="peer", note="big")
    monkeypatch.setenv("CTX_PRE_INVOCATION_STEP_FORMAT", "system_message")
    out = _run_hook(
        monkeypatch,
        hook.main_session_start,
        "antigravity",
        {"hookEventName": "PreInvocation", "workspacePaths": [str(ws)]},
    )
    step = out["injectSteps"][0]
    assert "systemMessage" in step
    assert step["systemMessage"]["renderInfo"] == {"hidden": True}


def test_mcp_meta_workspace_fallback():
    assert mcp._meta_workspace({"_meta": {"antigravity.google/workspace_paths": ["/ws/a", "/ws/b"]}}) == "/ws/a"
    assert mcp._meta_workspace({"_meta": {"workspacePaths": ["/ws/c"]}}) == "/ws/c"
    assert mcp._meta_workspace({"_meta": {"workspacePath": "/ws/d"}}) == "/ws/d"
    assert mcp._meta_workspace({}) is None
