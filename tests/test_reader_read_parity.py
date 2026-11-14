"""Shell readers reach the Read tool's decision for the same file.

The rule, for each file a reader opens FOR READING (Read tool or shell
reader), in order: a secret path asks (always, also over user allow rules);
anything that is not a regular file or directory asks (``/dev/null`` keeps
base's decision); inside the workspace (links resolved, named relatively or
absolutely) keeps base's decision; inside a directory the host handed this
session (its tool-results directory, the scratchpad the payload names) too;
anywhere else gets the outside decision unless the user allowed the command.
An output-redirect target is never a read, and what cannot be seen in the
command text (an environment variable, a substitution) keeps base's decision.
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
           "tac {}", "strings {}", "jq . {}", "bat {}", "zcat {}", "xxd {}")
SESSION = "s1"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    results = cfg / "projects" / "proj" / SESSION / "tool-results"
    results.mkdir(parents=True)
    (results / "b1.txt").write_text("saved\n", encoding="utf-8")
    other_results = cfg / "projects" / "proj" / "other-session" / "tool-results"
    other_results.mkdir(parents=True)
    (other_results / "b2.txt").write_text("saved\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "notes.txt").write_text("n\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / "a.txt").write_text("line 1\nline 2\n", encoding="utf-8")
    (ws / "sub").mkdir()
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
        "absolute workspace file": str(ws / "a.txt"),
        "link in": "link_in.txt",
        "tool results": str(results / "b1.txt"),
        "scratchpad": str(scratch / "notes.txt"),
        "another session's results": str(other_results / "b2.txt"),
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
    return {"ws": ws, "home": home, "classes": classes, "results": results,
            "scratch": scratch, "outside": outside, "tmp": tmp_path}


def _emit(flavor: str, payload: dict) -> dict:
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use(flavor)
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout


def _payload(flavor: str, world, *, command: str | None = None, path: str | None = None,
             cwd=None) -> dict:
    ws = world["ws"]
    cwd = str(cwd or ws)
    if flavor == "antigravity":
        name, args = (("run_command", {"CommandLine": command}) if command is not None
                      else ("read_file", {"Path": path}))
        return {"conversationId": "00000000-0000-4000-8000-000000000000", "cwd": cwd,
                "hookEventName": "PreToolUse", "workspacePaths": [str(ws)],
                "scratchpad_dir": str(world["scratch"]), "session_id": SESSION,
                "toolCall": {"name": name, "args": args, "id": "c1"}}
    tool, ti = (("Bash", {"command": command}) if command is not None
                else ("Read", {"file_path": path}))
    return {"session_id": SESSION, "cwd": cwd, "hook_event_name": "PreToolUse",
            "scratchpad_dir": str(world["scratch"]), "tool_name": tool, "tool_input": ti}


def _kind(flavor: str, out: dict) -> str:
    """ask | deny | pass: the permission outcome, whatever runs."""
    if flavor == "antigravity":
        d = out.get("decision")
        return "ask" if d in ("ask", "force_ask") else ("deny" if d == "deny" else "pass")
    d = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    return "ask" if d == "ask" else ("deny" if d == "deny" else "pass")


def _run(world, flavor, command, **kw):
    return _kind(flavor, _emit(flavor, _payload(flavor, world, command=command, **kw)))


def _read_rule_fired(out: dict) -> bool:
    text = json.dumps(out)
    return any(r in text for r in ("path resolves outside", "not a regular file",
                                   "secret-bearing path"))


def _abs(world, rel: str) -> str:
    if rel.startswith("~"):
        return str(world["home"]) + rel[1:]
    return rel if os.path.isabs(rel) else os.path.normpath(str(world["ws"] / rel))


ASKS = {"claude-code": "ask", "antigravity": "ask", "codex": "deny"}
PASS_CLASSES = ("workspace file", "absolute workspace file", "link in", "tool results",
                "scratchpad")


@pytest.mark.parametrize("flavor", HOSTS)
def test_shell_readers_match_the_read_tool_over_the_path_classes(world, flavor):
    mismatches = []
    for cls, rel in world["classes"].items():
        read = _kind(flavor, _emit(flavor, _payload(flavor, world, path=_abs(world, rel))))
        want_read = "pass" if cls in PASS_CLASSES else ASKS[flavor]
        if read != want_read:
            mismatches.append(("Read", cls, want_read, read))
        for tmpl in READERS:
            for prefix in ("", "sudo ", "sudo -u root ", "exec "):
                cmd = prefix + tmpl.format(rel)
                got = _run(world, flavor, cmd)
                if cls in PASS_CLASSES:
                    # base's own decision stands (a wrapper with options is
                    # an unknown command on Antigravity, as it was)
                    out = _emit(flavor, _payload(flavor, world, command=cmd))
                    if _read_rule_fired(out):
                        mismatches.append((cls, cmd, "base", got))
                elif got != read:
                    mismatches.append((cls, cmd, read, got))
    assert not mismatches, mismatches


AGY_UNCHANGED = {
    # command: emitted on Antigravity before and after (captured = rewritten
    # to `ctx run`)
    "cat a.txt": "captured", "cat link_in.txt": "captured", "sudo cat a.txt": "captured",
    "cat a.txt | wc -l": "captured", "cat a.txt > out.txt": "captured",
    "grep line a.txt | wc -l": "captured", "sed -n 1,5p a.txt": "captured",
    "head a.txt": "allow", "tail a.txt": "allow", "sort a.txt": "captured",
    "cat ../ws/a.txt": "captured", "cat a.*": "captured",
    "cat $HOME/notes.txt": "captured", 'head -n 2 "$F"': "captured",
    "cat $(echo /etc/shadow)": "captured", "cat $TMPDIR/x": "captured",
    "cat /dev/null": "captured", "cat < /dev/null": "captured",
    "cd $X && cat hosts": "captured", "gzip /tmp/x": "force_ask",
}


@pytest.mark.parametrize("command,want", sorted(AGY_UNCHANGED.items()))
def test_ordinary_and_dynamic_reads_keep_their_decision(world, command, want):
    out = _emit("antigravity", _payload("antigravity", world, command=command))
    got = ("captured" if "ctx" in str((out.get("overwrite") or {}).get("CommandLine", ""))
           else out["decision"])
    assert got == want, (command, out)
    for flavor in ("claude-code", "codex"):
        assert _run(world, flavor, command) == "pass", (flavor, command)


REDIRECT_OPS = (">", ">>", "2>", "&>", ">&", ">|", "1>", "2>>", "&>>")


@pytest.mark.parametrize("op", REDIRECT_OPS)
@pytest.mark.parametrize("space", [" ", ""])
@pytest.mark.parametrize("target", ["/dev/null", "/tmp/ctx-redir-test.out", "out.txt"])
def test_an_output_redirect_target_is_never_a_read(world, op, space, target):
    for cmd in (f"cat a.txt {op}{space}{target}", f"grep -q line a.txt {op}{space}{target} 2>&1",
                f"sort a.txt {op}{space}{target}"):
        for flavor in HOSTS:
            out = _emit(flavor, _payload(flavor, world, command=cmd))
            assert not _read_rule_fired(out), (flavor, cmd, out)
            if flavor != "antigravity":
                assert _kind(flavor, out) == "pass", (flavor, cmd)


@pytest.mark.parametrize("command", [
    "sudo cat /etc/shadow", "doas cat /etc/shadow", "sudo head -n 3 /etc/hosts",
    "sudo -u u cat /etc/shadow", "sudo -n cat /etc/hosts", "sudo -E cat /etc/hosts",
    "sudo -u u -- cat /etc/hosts", "exec cat /etc/hosts", "builtin cat /etc/hosts",
    "ionice -c 3 cat /etc/hosts", "nice -n 5 cat /etc/hosts", "env -i cat /etc/hosts",
    "timeout -k 1 5 cat /etc/hosts",
    "cat link_out.txt", "cat /dev/zero", "cat /dev/urandom", "cat /dev/stdin",
    "cat pipe.fifo", "cat /etc/hosts | wc -l", "cat /etc/hosts > out.txt 2>&1",
    "ctx run -- cat /etc/hosts", "sort < /etc/hosts", "sort </etc/hosts",
    "cat {a.txt,/etc/hosts}", "grep -r line /etc", "tail -n 3 ../outside.log",
    "grep -f /etc/hosts a.txt", "grep --file=/etc/hosts a.txt", "grep --file /etc/hosts a.txt",
    "grep -f/etc/hosts a.txt", "awk -f /etc/prog.awk a.txt",
    "cd /etc && cat hosts", "cd .. && cat outside.log", "cd ~ && cat notes.txt",
    "f=/etc/hosts; cat $f", 'f=/etc/hosts && grep x "$f"', "export F=/etc/hosts; cat $F",
    'for f in a.txt /etc/hosts; do cat "$f"; done', "cat link_out.txt*",
    "diff a.txt /etc/hosts", "jq . /etc/x.json", "yq e .a /etc/x.yml", "zcat /tmp/x.gz",
    "gzip -dc /etc/x.gz", "gzip -cd /etc/x.gz", "bzcat /etc/x.bz2", "xzcat /etc/x.xz",
    "xxd /etc/hosts", "od -c /etc/hosts", "hexdump -C /etc/hosts", "bat /etc/hosts",
])
def test_reads_the_read_tool_asks_about_ask_on_every_host(world, command):
    for flavor in HOSTS:
        out = _emit(flavor, _payload(flavor, world, command=command))
        assert _kind(flavor, out) == ASKS[flavor], (flavor, command, out)
        if flavor == "antigravity":
            assert "overwrite" not in out, out  # never captured


@pytest.mark.parametrize("command", [
    "cat .env*", "cat .e?v", "cat .[e]nv", 'for f in a.txt .env; do cat "$f"; done',
    "f=.env; head $f",
])
def test_secret_paths_through_globs_and_variables(world, command):
    for flavor in HOSTS:
        out = _emit(flavor, _payload(flavor, world, command=command))
        assert _kind(flavor, out) == ASKS[flavor], (flavor, command, out)
        text = json.dumps(out)
        assert "secret-bearing path" in text, (flavor, command, out)


@pytest.mark.parametrize("command", [
    'for f in a.txt link_in.txt; do cat "$f"; done', 'f=a.txt; grep x "$f"',
    "f=sub/../a.txt && head -3 $f", "cd sub && cat ../a.txt", "cat {RESULTS}",
    'f={RESULTS}; grep -n saved "$f"', "tail -n 3 {SCRATCH}", "cd {SCRATCH_DIR} && cat notes.txt",
    "cat {WS}/a.txt", "command -v cat",
])
def test_workspace_and_host_handed_files_keep_base(world, command):
    command = (command.replace("{RESULTS}", str(world["results"] / "b1.txt"))
               .replace("{SCRATCH_DIR}", str(world["scratch"]))
               .replace("{SCRATCH}", str(world["scratch"] / "notes.txt"))
               .replace("{WS}", str(world["ws"])))
    for flavor in ("claude-code", "codex"):
        assert _run(world, flavor, command) == "pass", (flavor, command)
    assert not _read_rule_fired(_emit("antigravity", _payload("antigravity", world,
                                                               command=command))), command


def test_tool_results_of_another_session_are_outside(world):
    path = str(world["tmp"] / "cfg" / "projects" / "proj" / "other-session" / "tool-results" / "b2.txt")
    for flavor in ("claude-code", "codex"):
        assert _run(world, flavor, f"cat {path}") == ASKS[flavor]
        assert _kind(flavor, _emit(flavor, _payload(flavor, world, path=path))) == ASKS[flavor]


def test_the_read_tool_trusts_the_hosts_own_directories(world):
    for flavor in ("claude-code", "codex"):
        for path in (world["results"] / "b1.txt", world["scratch"] / "notes.txt"):
            assert _kind(flavor, _emit(flavor, _payload(flavor, world, path=str(path)))) == "pass"


def test_the_read_tool_asks_about_a_fifo_in_the_workspace(world):
    # Changed on purpose: base passed a Read of a FIFO, which may never end.
    for flavor in HOSTS:
        out = _emit(flavor, _payload(flavor, world, path=str(world["ws"] / "pipe.fifo")))
        assert _kind(flavor, out) == ASKS[flavor]


def test_in_place_edit_protection_runs_first(world):
    (world["ws"] / "t").mkdir()
    (world["ws"] / "t" / "x.py").write_text("a\n", encoding="utf-8")
    command = "f=t/x.py && sed -i 's/a/b/' $f && head -8 $f"
    for flavor in HOSTS:
        out = _emit(flavor, _payload(flavor, world, command=command))
        assert _kind(flavor, out) == ASKS[flavor], (flavor, out)
        assert "in-place text edit" in json.dumps(out), (flavor, out)


def test_payload_cwd_resolves_relative_paths(world):
    sub = world["ws"] / "sub"
    for flavor in ("claude-code", "codex"):
        assert _run(world, flavor, "cat ../a.txt", cwd=sub) == "pass"
        assert _run(world, flavor, "cat ../../outside.log", cwd=sub) == ASKS[flavor]


def test_sudo_judges_a_path_the_caller_cannot_inspect_as_outside(world, monkeypatch):
    hidden = str(world["ws"] / "sub" / "root-only.txt")
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        if os.fspath(path) == hidden:
            raise PermissionError(13, "Permission denied", path)
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(hook.os, "stat", fake_stat)
    assert _run(world, "claude-code", f"cat {hidden}") == "pass"
    assert _run(world, "claude-code", f"sudo cat {hidden}") == "ask"
    assert _run(world, "claude-code", f"sudo -u root cat {hidden}") == "ask"


def test_user_allow_rules_keep_base_precedence(world):
    # Claude Code settings allow `cat`: the outside ask is withdrawn ...
    claude = world["tmp"] / "cfg"
    (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(cat:*)"]}}))
    assert _run(world, "claude-code", "cat /etc/hosts") == "pass"
    # ... but never for a secret path.
    assert _run(world, "claude-code", "cat .env") == "ask"
    (claude / "settings.json").write_text("{}")
    # allow_commands in ctx.toml: base's allow on every host.
    (world["ws"] / "ctx.toml").write_text(
        'version = 1\n[guard]\nallow_commands = ["cat /etc/hosts"]\n', encoding="utf-8")
    for flavor in HOSTS:
        assert _run(world, flavor, "cat /etc/hosts") == "pass", flavor
        assert _run(world, flavor, "cat .env") == ASKS[flavor], flavor


def test_reader_operands_skip_patterns_and_option_values():
    from ctx.hook import _reader_operands

    assert _reader_operands("grep", ["grep", "-n", "/usr/x", "a.txt"]) == ["a.txt"]
    assert _reader_operands("grep", ["grep", "-e", "/usr/x", "a", "b"]) == ["a", "b"]
    assert _reader_operands("grep", ["grep", "-f", "/p", "a"]) == ["/p", "a"]
    assert _reader_operands("grep", ["grep", "--file=/p", "a"]) == ["/p", "a"]
    assert _reader_operands("grep", ["grep", "--file", "/p", "a"]) == ["/p", "a"]
    assert _reader_operands("sed", ["sed", "-n", "1,5p", "f"]) == ["f"]
    assert _reader_operands("awk", ["awk", "-F", "/", "{print}", "x=1", "f"]) == ["f"]
    assert _reader_operands("cut", ["cut", "-d", "/", "-f1", "f"]) == ["f"]
    assert _reader_operands("head", ["head", "-n", "5", "f"]) == ["f"]
    assert _reader_operands("tail", ["tail", "--lines=5", "--", "-f"]) == ["-f"]
    assert _reader_operands("jq", ["jq", "--slurpfile", "x", "/s", ".", "f"]) == ["/s", "f"]


def test_redirects_are_split_from_operands():
    from ctx.hook import _split_redirects

    assert _split_redirects(["cat", "a", ">", "/tmp/x", "2>&1"]) == (["cat", "a"], [])
    assert _split_redirects(["cat", "a", ">/tmp/x", "2>/e"]) == (["cat", "a"], [])
    assert _split_redirects(["sort", "<", "/etc/p"]) == (["sort"], ["/etc/p"])
    assert _split_redirects(["sort", "</etc/p", "<<<", "x"]) == (["sort"], ["/etc/p"])
