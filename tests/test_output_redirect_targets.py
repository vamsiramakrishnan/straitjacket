"""An output redirect target is never a read, whatever it is written as.

For every output operator, with and without a space, and every target form
(a literal path outside the workspace, quoted, `$TMPDIR/x`, `"$PWD/../x"`,
`$HOME/x`, `~/x`, `$(mktemp)`), a reader and a non-reader command get the
decision the same command gets without the redirect (and the Read rule's
outside reason never appears). `tee FILE` writes FILE: not a read either.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from ctx import hook

HOSTS = ("claude-code", "antigravity", "codex")
OPS = (">", ">>", "2>", "&>", ">&", ">|", "1>", "2>>", "&>>")
TARGETS = ("/etc/ctx-out.log", '"/etc/ctx out.log"', "$TMPDIR/x", '"$PWD/../x"', "$HOME/x",
           "~/x", "${TMPDIR:-/tmp}/x")
COMMANDS = ("cat a.txt", "./ci.sh", "gzip -dc a.gz", "grep -n x a.txt")


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(hook, "_account_home", lambda: str(tmp_path / "home"))
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "ctx.toml").write_text("version = 1\n")
    (root / "a.txt").write_text("x\n")
    return root


def _emit(flavor, ws, command):
    if flavor == "antigravity":
        payload = {"conversationId": "c", "cwd": str(ws), "hookEventName": "PreToolUse",
                   "workspacePaths": [str(ws)],
                   "toolCall": {"name": "run_command", "args": {"CommandLine": command}, "id": "1"}}
    else:
        payload = {"session_id": "s", "cwd": str(ws), "hook_event_name": "PreToolUse",
                   "tool_name": "Bash", "tool_input": {"command": command}}
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use(flavor)
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout


def _decision(flavor, out):
    if flavor == "antigravity":
        return out.get("decision"), "overwrite" in out
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision"), None


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("space", [" ", ""])
@pytest.mark.parametrize("target", TARGETS)
def test_an_output_target_never_changes_the_decision(ws, op, space, target):
    for command in COMMANDS:
        for flavor in HOSTS:
            plain = _emit(flavor, ws, command)
            redirected = _emit(flavor, ws, f"{command} {op}{space}{target}")
            assert "path resolves outside" not in json.dumps(redirected), (flavor, command, op, target)
            if flavor != "antigravity":
                # Antigravity's base answer for a redirect is its own volume
                # rule (a redirect proves the console small); the guard's
                # permission outcome is what must not move.
                assert _decision(flavor, redirected) == _decision(flavor, plain), (
                    flavor, command, op, target, plain, redirected)


@pytest.mark.parametrize("op", OPS)
def test_a_substituted_output_target_is_not_a_read(ws, op):
    for command in COMMANDS:
        for flavor in HOSTS:
            out = _emit(flavor, ws, f"{command} {op} $(mktemp)")
            assert "path resolves outside" not in json.dumps(out), (flavor, command, op)


@pytest.mark.parametrize("target", ["/etc/ctx-out.log", "$TMPDIR/x", "$HOME/x", "~/x"])
def test_a_tee_target_is_not_a_read(ws, target):
    for flavor in HOSTS:
        out = _emit(flavor, ws, f"cat a.txt | tee {target}")
        assert "path resolves outside" not in json.dumps(out), (flavor, target, out)


@pytest.mark.parametrize("write,read", [
    ('./ci.sh > "$TMPDIR/ci_full.log" 2>&1', 'grep -n x "${TMPDIR:-/tmp}/ci_full.log"'),
    ('gzip -dc a.gz > "$PWD/../ci-job-output.log"', 'wc -l "$PWD/../ci-job-output.log"; '
                                                    'grep -n x "$PWD/../ci-job-output.log"'),
])
def test_the_replay_commands_ask_for_their_later_read_not_the_write(ws, write, read):
    # The final-replay "regressions": the write alone is not judged; the
    # same command's later READ of that file outside the workspace is (the
    # `${TMPDIR:-/tmp}` default and `$PWD/..` are resolved, round-2 item 3).
    for flavor in HOSTS:
        assert "path resolves outside" not in json.dumps(_emit(flavor, ws, write))
        assert "path resolves outside" in json.dumps(_emit(flavor, ws, f"{write}; {read}"))
