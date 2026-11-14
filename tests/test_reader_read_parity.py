"""Shell readers reach the Read tool's decision for the same file.

The Read guard asks before a secret path, a path outside the workspace
(links resolved, devices and kernel views included) and, inside it, a file
that is not a regular file (a FIFO, a device). ``cat``, ``head``, ``grep``,
``sed -n``, ``sort`` and the other readers used to be captured or run for
those same files on Antigravity -- through a link out, under ``sudo`` -- and
passed on Claude Code and Codex. They now get exactly the Read decision,
per host, no stricter and no looser; a workspace file, a pipeline and a
redirect keep the decision they had.
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from ctx import hook

HOSTS = ("claude-code", "antigravity", "codex")
READERS = ("cat {}", "head -n 5 {}", "tail {}", "less {}", "more {}", "grep line {}",
           "awk '{{print}}' {}", "sed -n 1,5p {}", "sort {}", "nl {}", "cut -c1 {}",
           "tac {}", "strings {}")


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / "a.txt").write_text("line 1\nline 2\n", encoding="utf-8")
    (ws / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n", encoding="utf-8")
    (home / "notes.txt").write_text("notes\n", encoding="utf-8")
    (ws / "link_out.txt").symlink_to(outside)
    (ws / "link_in.txt").symlink_to("a.txt")
    (ws / "link_secret.txt").symlink_to(".env")
    os.mkfifo(ws / "pipe.fifo")
    classes = {
        "workspace file": "a.txt",
        "link in": "link_in.txt",
        "outside /tmp": str(outside),
        "outside home": "~/notes.txt",
        "outside etc": "/etc/hosts",
        "link out": "link_out.txt",
        "secret": ".env",
        "link to secret": "link_secret.txt",
        "device": "/dev/zero",
        "stdin device": "/dev/stdin",
        "fifo": "pipe.fifo",
        "parent of workspace": "../outside.log",
    }
    return {"ws": ws, "home": home, "classes": classes}


def _emit(flavor: str, payload: dict) -> dict:
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use(flavor)
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout


def _payload(flavor: str, ws, *, command: str | None = None, path: str | None = None) -> dict:
    if flavor == "antigravity":
        name, args = (("run_command", {"CommandLine": command}) if command is not None
                      else ("read_file", {"Path": path}))
        return {"conversationId": "00000000-0000-4000-8000-000000000000", "cwd": str(ws),
                "hookEventName": "PreToolUse", "workspacePaths": [str(ws)],
                "toolCall": {"name": name, "args": args, "id": "c1"}}
    tool, ti = (("Bash", {"command": command}) if command is not None
                else ("Read", {"file_path": path}))
    return {"session_id": "s1", "cwd": str(ws), "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": ti}


def _kind(flavor: str, out: dict) -> str:
    """ask | deny | pass: the permission outcome, whatever runs."""
    if flavor == "antigravity":
        d = out.get("decision")
        return "ask" if d in ("ask", "force_ask") else ("deny" if d == "deny" else "pass")
    d = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    return "ask" if d == "ask" else ("deny" if d == "deny" else "pass")


def _abs(world, rel: str) -> str:
    if rel.startswith("~"):
        return str(world["home"]) + rel[1:]
    return rel if os.path.isabs(rel) else os.path.normpath(str(world["ws"] / rel))


@pytest.mark.parametrize("flavor", HOSTS)
def test_shell_readers_match_the_read_tool_over_the_path_classes(world, flavor):
    ws = world["ws"]
    mismatches = []
    for cls, rel in world["classes"].items():
        read = _kind(flavor, _emit(flavor, _payload(flavor, ws, path=_abs(world, rel))))
        for tmpl in READERS:
            for prefix in ("", "sudo "):
                cmd = prefix + tmpl.format(rel)
                got = _kind(flavor, _emit(flavor, _payload(flavor, ws, command=cmd)))
                if prefix == "" and cls in ("workspace file", "link in"):
                    # The Read tool allows these; a reader keeps its own decision
                    # (captured, run natively, or the pager's deny on agy).
                    if got == "ask":
                        mismatches.append((cls, cmd, read, got))
                    continue
                if prefix and cls in ("workspace file", "link in"):
                    plain = _kind(flavor, _emit(flavor, _payload(
                        flavor, ws, command=tmpl.format(rel))))
                    if got != plain:  # sudo never changes the decision
                        mismatches.append((cls, cmd, plain, got))
                    continue
                if got != read:
                    mismatches.append((cls, cmd, read, got))
    assert not mismatches, mismatches


def test_the_read_tool_decisions_per_host(world):
    """What the Read guard decides, pinned per host: ask on Claude Code and
    Antigravity, deny on Codex (it has no ask at this hook)."""
    ws = world["ws"]
    want = {"claude-code": "ask", "antigravity": "ask", "codex": "deny"}
    for flavor in HOSTS:
        for cls in ("outside /tmp", "outside home", "outside etc", "link out", "secret",
                    "device", "fifo"):
            got = _kind(flavor, _emit(flavor, _payload(
                flavor, ws, path=_abs(world, world["classes"][cls]))))
            assert got == want[flavor], (flavor, cls, got)
        assert _kind(flavor, _emit(flavor, _payload(
            flavor, ws, path=str(ws / "a.txt")))) == "pass"


AGY_UNCHANGED = {
    # command: emitted on Antigravity before and after (captured = rewritten
    # to `ctx run`)
    "cat a.txt": "captured", "cat link_in.txt": "captured", "sudo cat a.txt": "captured",
    "cat a.txt | wc -l": "captured", "cat a.txt > out.txt": "captured",
    "grep line a.txt | wc -l": "captured", "sed -n 1,5p a.txt": "captured",
    "head a.txt": "allow", "tail a.txt": "allow", "sort a.txt": "captured",
    "cat ../ws/a.txt": "captured", "cat *.txt": "captured",
}


@pytest.mark.parametrize("command,want", sorted(AGY_UNCHANGED.items()))
def test_ordinary_reads_keep_their_decision(world, command, want):
    out = _emit("antigravity", _payload("antigravity", world["ws"], command=command))
    got = ("captured" if "ctx" in str((out.get("overwrite") or {}).get("CommandLine", ""))
           else out["decision"])
    assert got == want, (command, out)


@pytest.mark.parametrize("command", [
    "sudo cat /etc/shadow", "doas cat /etc/shadow", "sudo head -n 3 /etc/hosts",
    "cat link_out.txt", "cat /dev/zero", "cat /dev/urandom", "cat /dev/stdin",
    "cat pipe.fifo", "cat /etc/hosts | wc -l", "cat /etc/hosts > out.txt 2>&1",
    "ctx run -- cat /etc/hosts", "sort < /etc/hosts", "cat {a.txt,/etc/hosts}",
    "grep -r line /etc", "grep -f /etc/hosts a.txt", "tail -n 3 ../outside.log",
])
def test_reads_the_read_tool_asks_about_ask_on_every_host(world, command):
    for flavor in HOSTS:
        out = _emit(flavor, _payload(flavor, world["ws"], command=command))
        assert _kind(flavor, out) == ("deny" if flavor == "codex" else "ask"), (flavor, out)
        if flavor == "antigravity":
            assert "overwrite" not in out, out  # never captured


@pytest.mark.parametrize("command", ["cat $HOME/notes.txt", 'head -n 2 "$F"', "cat `echo x`",
                                     "cat $(echo /etc/shadow)", "sort $(echo a.txt) | wc -l",
                                     "cat $HOME/notes.txt | wc -l"])
def test_a_path_that_is_not_literal_is_never_captured(world, command):
    out = _emit("antigravity", _payload("antigravity", world["ws"], command=command))
    assert out["decision"] == "force_ask" and "overwrite" not in out, out


def test_reader_operands_skip_patterns_and_option_values():
    from ctx.hook import _reader_operands

    assert _reader_operands("grep", ["grep", "-n", "/usr/x", "a.txt"]) == ["a.txt"]
    assert _reader_operands("grep", ["grep", "-e", "/usr/x", "a", "b"]) == ["a", "b"]
    assert _reader_operands("grep", ["grep", "-f", "/p", "a"]) == ["/p", "a"]
    assert _reader_operands("sed", ["sed", "-n", "1,5p", "f"]) == ["f"]
    assert _reader_operands("awk", ["awk", "-F", "/", "{print}", "x=1", "f"]) == ["f"]
    assert _reader_operands("cut", ["cut", "-d", "/", "-f1", "f"]) == ["f"]
    assert _reader_operands("head", ["head", "-n", "5", "f"]) == ["f"]
    assert _reader_operands("tail", ["tail", "--lines=5", "--", "-f"]) == ["-f"]
