"""Acceptance: hook JSON contract, classification policy, fail-open."""

import json

import pytest
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _invoke_hook(payload: str, env_extra=None) -> dict:
    """Run the real hook entry end-to-end (stdin JSON → stdout JSON)."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", "antigravity", "pre-tool-use"],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    assert len(lines) == 1, f"hook must emit exactly one JSON object, got: {proc.stdout!r}"
    return json.loads(lines[0])


def _classify(tool_name, tool_input, workspace=None):
    from ctx.hook import classify

    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if workspace:
        payload["workspacePaths"] = [str(workspace)]
    return classify(payload)


def _steering_deny(workspace):
    """Pin the workspace to the pure deny-with-remediation contract so the
    deny assertions below stay covered verbatim (steering off)."""
    (workspace / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )


def test_hook_emits_single_json_on_garbage_stdin():
    decision = _invoke_hook("this is not json {{{")
    assert decision == {"decision": "allow"}  # fail-open in default mode


def test_hook_emits_single_json_on_empty_stdin():
    decision = _invoke_hook("")
    assert decision == {"decision": "allow"}


def test_deny_unbounded_command_with_remediation(tmp_path):
    _steering_deny(tmp_path)
    d = _classify("run_command", {"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    assert d["decision"] == "deny"
    assert "ctx run -- pytest -q" in d["reason"]
    assert set(d) == {"decision", "reason"}  # no rewrite attached under steering=deny


def test_allow_ctx_routed_command(tmp_path):
    d = _classify("run_command", {"CommandLine": "ctx run -- pytest -q"}, tmp_path)
    assert d["decision"] == "allow"


def test_allow_bounded_commands(tmp_path):
    for cmd in ("pwd", "git status --short", "head -n 40 file.txt", "echo hi"):
        d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
        assert d["decision"] == "allow", cmd


def test_deny_unbounded_git_and_cat(tmp_path):
    _steering_deny(tmp_path)
    for cmd in ("git log", "git diff", "cat big.log", "find . -name '*.py'", "rg pattern"):
        d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
        assert d["decision"] == "deny", cmd


def test_pipeline_with_head_is_not_auto_safe(tmp_path):
    _steering_deny(tmp_path)
    d = _classify("run_command", {"CommandLine": "cat huge.log | head -n 5"}, tmp_path)
    assert d["decision"] == "force_ask"


def test_force_ask_secret_path(tmp_path):
    d = _classify("Read", {"file_path": str(tmp_path / ".env")}, tmp_path)
    assert d["decision"] == "force_ask"
    assert ".env" not in d["reason"] or "secret" in d["reason"].lower()


def test_force_ask_outside_workspace_read(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    d = _classify("Read", {"file_path": str(outside)}, ws)
    assert d["decision"] == "force_ask"


def test_deny_large_file_read(tmp_path):
    _steering_deny(tmp_path)
    big = tmp_path / "big.txt"
    big.write_text("x" * 20000, encoding="utf-8")
    d = _classify("Read", {"file_path": str(big)}, tmp_path)
    assert d["decision"] == "deny"
    assert "ctx get" in d["reason"]


def test_allow_small_file_read(tmp_path):
    small = tmp_path / "small.txt"
    small.write_text("hello", encoding="utf-8")
    d = _classify("Read", {"file_path": str(small)}, tmp_path)
    assert d["decision"] == "allow"


def test_longest_workspace_path_wins(tmp_path):
    from ctx.hook import _resolve_workspace_root

    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    root = _resolve_workspace_root(
        {
            "tool_input": {"Cwd": str(inner)},
            "workspacePaths": [str(outer), str(inner)],
        }
    )
    assert root == str(inner)


def test_multi_root_without_disambiguation_is_conservative(tmp_path):
    from ctx.hook import _resolve_workspace_root

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    root = _resolve_workspace_root({"tool_input": {}, "workspacePaths": [str(a), str(b)]})
    assert root is None  # no guess; policy falls back to defaults


def test_advisory_mode_allows_everything(tmp_path):
    (tmp_path / "ctx.toml").write_text('version = 1\n[guard]\nmode = "advisory"\n', encoding="utf-8")
    d = _classify("run_command", {"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    assert d["decision"] == "allow"


def test_fail_closed_when_configured(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\ninternal_error = "deny"\n', encoding="utf-8"
    )
    payload = json.dumps(
        {
            "tool_name": "run_command",
            "tool_input": {"CommandLine": None, "Cwd": str(tmp_path)},
            "workspacePaths": [str(tmp_path)],
        }
    )
    # CommandLine None → classify treats as empty command → allow; craft a
    # real internal error instead by making tool_input a bad type after parse.
    from ctx import hook

    original = hook.classify
    try:
        def boom(payload):
            raise RuntimeError("boom")

        hook.classify = boom
        import io
        import sys as _sys

        stdin, stdout = _sys.stdin, _sys.stdout
        _sys.stdin = io.StringIO(payload)
        _sys.stdout = io.StringIO()
        try:
            hook.main_pre_tool_use()
            out = _sys.stdout.getvalue()
        finally:
            _sys.stdin, _sys.stdout = stdin, stdout
    finally:
        hook.classify = original
    decision = json.loads(out)
    assert decision["decision"] == "deny"


def test_interpreter_bypass_channel_denied(tmp_path):
    d = _classify(
        "run_command",
        {"CommandLine": "python -c \"print(open('big.log').read())\"", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] in ("deny", "force_ask")


def test_claude_code_flavor_schema(tmp_path):
    import subprocess as sp

    _steering_deny(tmp_path)
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "cwd": str(tmp_path)}
    )
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = sp.run(
        [sys.executable, "-m", "ctx", "hook", "claude-code", "pre-tool-use"],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    assert len(lines) == 1
    out = json.loads(lines[0])
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "ctx run -- pytest -q" in hso["permissionDecisionReason"]


def test_claude_code_flavor_allow_is_valid():
    from ctx.hook import _to_claude_code_schema

    out = _to_claude_code_schema({"decision": "allow"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    out2 = _to_claude_code_schema({"decision": "force_ask", "reason": "why"})
    assert out2["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_codex_plain_allow_emits_no_unsupported_decision():
    from ctx.hook import _to_codex_schema

    assert _to_codex_schema({"decision": "allow"}) == {}


def test_codex_rewrite_is_allow_with_updated_input():
    from ctx.hook import _to_codex_schema

    out = _to_codex_schema(
        {
            "decision": "allow",
            "rewrite": {
                "updatedInput": {"command": "ctx run -- pytest -q"},
                "reason": "contain output",
            },
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"] == {"command": "ctx run -- pytest -q"}


def test_codex_force_ask_degrades_safely_to_deny():
    from ctx.hook import _to_codex_schema

    out = _to_codex_schema({"decision": "force_ask", "reason": "review this"})
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "review this"


# ------------------------------------------------ container escape surfaces
# headlong's docker broker denylist, taken as a safety-class force_ask: a
# privileged container, a host namespace, a host device, the engine socket
# or a bind mount from outside the workspace reach past the confinement the
# rest of the guard enforces. One predicate, every door.
def _cmd(command, ws):
    return _classify("run_command", {"CommandLine": command, "Cwd": str(ws)}, ws)


@pytest.mark.parametrize("command", [
    "docker run --privileged img",
    "docker exec --privileged c sh",
    "docker run --network host img",
    "docker run --pid=host img",
    "podman run --uts=host img",
    "docker run --device /dev/sda img",
    "docker run --cap-add=SYS_ADMIN img",
    "docker run -v /:/host img",
    "docker run -v ../:/up img",
    "docker run -v /var/run/docker.sock:/var/run/docker.sock img",
    "docker run --mount type=bind,source=/etc,target=/x img",
])
def test_container_escape_surfaces_force_ask(tmp_path, command):
    d = _cmd(command, tmp_path)
    assert d["decision"] == "force_ask", (command, d)
    assert "container escape surface" in d["reason"]


def test_unresolved_shell_variable_mount_still_asks(tmp_path):
    """`$HOME` is a shell metacharacter, so the compound-expression door
    answers first; the predicate itself refuses to guess what the variable
    holds. Either way the user sees it."""
    from ctx.hook import _container_escape

    assert _cmd("docker run -v $HOME/.ssh:/root/.ssh img", tmp_path)["decision"] == "force_ask"
    why = _container_escape(["docker", "run", "-v", "$HOME/.ssh:/root/.ssh", "img"], str(tmp_path))
    assert why and "cannot resolve" in why
    assert _container_escape(["docker", "run", "-v", "$PWD/data:/data", "img"], str(tmp_path)) is None


@pytest.mark.parametrize("command", [
    "docker ps",
    "docker compose up",
    "docker run -v named:/data img",
    "docker run -v ./data:/data img",
    "docker run --cap-add NET_BIND_SERVICE img",
    "docker run --mount type=bind,source={ws}/data,target=/x img",
])
def test_in_workspace_container_use_keeps_its_volume_class_answer(tmp_path, command):
    (tmp_path / "data").mkdir()
    _steering_deny(tmp_path)
    d = _cmd(command.format(ws=tmp_path), tmp_path)
    assert d["decision"] == "deny", (command, d)  # unbounded output, ctx run remediation
    assert "container escape" not in d["reason"]


@pytest.mark.parametrize("command", [
    "docker run --privileged img > out.log 2>&1",       # the redirect shortcut
    "echo hi && docker run --privileged img",           # a chain segment
    "ctx run -- docker run --privileged img",           # the deny remediation's own door
    "ctx run --shell -- 'docker run --privileged img | head -5'",
])
def test_container_escape_is_seen_through_every_door(tmp_path, command):
    d = _cmd(command, tmp_path)
    assert d["decision"] == "force_ask", (command, d)
    assert "container escape surface" in d["reason"]


def test_ctx_run_door_keeps_the_secret_path_guarantee(tmp_path):
    """`ctx run -- cat secrets.json` used to be allowed outright because the
    program was `ctx`; capture answers the volume question, never the
    safety one."""
    assert _cmd("ctx run -- cat secrets.json", tmp_path)["decision"] == "force_ask"
    assert _cmd("ctx run -- pytest -q", tmp_path)["decision"] == "allow"
