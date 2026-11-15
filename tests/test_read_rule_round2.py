"""Read rule, review round 2: strictest over every part, secrets over every
allow source, globs, forms visible in the text, host allow layers, and the
false asks the replay found. Each test failed before its fix."""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from ctx import hook

HOSTS = ("claude-code", "antigravity", "codex")
ASK = {"claude-code": "ask", "antigravity": "ask", "codex": "deny"}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "k1").write_text("key\n")
    for i in range(70):  # more than the 64 matches round 1 expanded
        (home / ".ssh" / f"pub{i}").write_text("k\n")
    (home / "notes.txt").write_text("n\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(hook, "_account_home", lambda: str(home), raising=False)
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    (ws / "ctx.toml").write_text("version = 1\n")
    (ws / "a.txt").write_text("a\n")
    (ws / ".env").write_text("T=1\n")
    (ws / "id_rsa").write_text("k\n")
    (ws / "sub").mkdir()
    (ws / "link_sec.txt").symlink_to(".env")
    many = ws / "many2"
    many.mkdir()
    # More than 64 matches for every pattern used below: round 1 judged at
    # most 64 glob matches and kept base past that, so these fail there.
    for i in range(80):
        (many / f"f{i:02d}.txt").write_text("x\n")
        (many / f"c{i:02d}.json").write_text("{}\n")
        (many / f"z{i:02d}.txt").write_text("x\n")
    (many / "credentials.json").write_text("{}\n")
    (many / "zlink").symlink_to("/etc/passwd")
    outside_dir = tmp_path / "elsewhere" / "deep" / "dir"
    outside_dir.mkdir(parents=True)
    return {"ws": ws, "home": home, "cfg": cfg, "tmp": tmp_path, "outside": outside_dir}


def _emit(flavor, payload):
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use(flavor)
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout


def _payload(flavor, w, command=None, path=None, **extra):
    ws = str(w["ws"])
    if flavor == "antigravity":
        name, args = (("run_command", {"CommandLine": command}) if command is not None
                      else ("read_file", {"Path": path}))
        return {"conversationId": "c", "cwd": ws, "hookEventName": "PreToolUse",
                "workspacePaths": [ws], "toolCall": {"name": name, "args": args, "id": "1"},
                **extra}
    tool, ti = (("Bash", {"command": command}) if command is not None
                else ("Read", {"file_path": path}))
    return {"session_id": "s1", "cwd": ws, "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": ti, **extra}


def _kind(flavor, out):
    if flavor == "antigravity":
        d = out.get("decision")
        return "ask" if d in ("ask", "force_ask") else ("deny" if d == "deny" else "pass")
    d = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    return "ask" if d == "ask" else ("deny" if d == "deny" else "pass")


def _run(w, flavor, command, **extra):
    return _emit(flavor, _payload(flavor, w, command, **extra))


def _fired(out):
    text = json.dumps(out)
    return any(r in text for r in ("path resolves outside", "not a regular file",
                                   "secret-bearing path"))


def _allow_everywhere(w):
    """Every allow source for `cat`: Claude Code's three layers, ctx's own
    allow_commands and an Antigravity rule."""
    rule = {"permissions": {"allow": ["Bash(cat:*)", "Bash(tail:*)"]}}
    (w["cfg"] / "settings.json").write_text(json.dumps(rule))
    (w["ws"] / ".claude").mkdir(exist_ok=True)
    (w["ws"] / ".claude" / "settings.json").write_text(json.dumps(rule))
    (w["ws"] / ".claude" / "settings.local.json").write_text(json.dumps(rule))
    (w["ws"] / "ctx.toml").write_text('version = 1\n[guard]\nallow_commands = ["cat", "tail"]\n')
    agy = w["home"] / ".gemini" / "antigravity-cli" / "settings.json"
    agy.parent.mkdir(parents=True, exist_ok=True)
    agy.write_text(json.dumps({"permissions": {"allow": ["command(cat)", "command(tail)"]}}))


# ------------------------------------------------------------------ item 1
SECRET_COMPOUNDS = ["cat /etc/passwd; cat .env", "cat /etc/passwd | cat .env",
                    "cat /etc/passwd && cat .env", "cat /etc/passwd | cat link_sec.txt",
                    "cat /etc/passwd | cat ~/.ssh/k1", "cat /etc/passwd | cat id_rsa",
                    "cat /etc/passwd | tail .env", "cat .env; cat /etc/passwd"]


@pytest.mark.parametrize("command", ["cat /etc/passwd; cat .env", "cat /etc/passwd | cat .env",
                                     "cat /etc/passwd && cat .env"])
def test_a_secret_in_a_later_part_wins_over_a_user_allow_rule(world, monkeypatch, command):
    # The round-2 blocker as reported: ~/.claude/settings.json (the user
    # layer, no CLAUDE_CONFIG_DIR) allows Bash(cat:*). On 08aeb79 the first
    # part's outside ask won the compound and the allow withdrew it.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    claude = world["home"] / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(cat:*)"]}}))
    out = _run(world, "claude-code", command)
    assert _kind("claude-code", out) == "ask", out
    assert "secret-bearing path" in json.dumps(out), out


@pytest.mark.parametrize("command", SECRET_COMPOUNDS)
def test_a_secret_in_any_part_wins_over_every_allow_source(world, command):
    # Regression guard over all sources at once (it also holds on 08aeb79);
    # the failing-first case is the user-layer test above.
    _allow_everywhere(world)
    for flavor in HOSTS:
        out = _run(world, flavor, command)
        assert _kind(flavor, out) == ASK[flavor], (flavor, command, out)
        assert "secret-bearing path" in json.dumps(out), (flavor, command, out)


def test_without_allows_a_compound_takes_its_strictest_part(world):
    for flavor in HOSTS:
        out = _run(world, flavor, "cat /etc/passwd | head -3; cat .env")
        assert "secret-bearing path" in json.dumps(out), (flavor, out)


# ------------------------------------------------------------------ item 2
@pytest.mark.parametrize("command", ["cat /etc/*", "cat /etc/[a-z]*", "cat ~/.ssh/*",
                                     "for f in /etc/*; do cat $f; done", "cat /etc/*/*"])
def test_a_glob_whose_directory_is_outside_is_outside(world, command):
    # `cat ~/.ssh/*` also held before (the literal `.ssh/` is a secret name).
    for flavor in HOSTS:
        assert _kind(flavor, _run(world, flavor, command)) == ASK[flavor], (flavor, command)


@pytest.mark.parametrize("command", ["cat many2/*", "cat many2/*.json", "cat many2/c*",
                                     "head -n 1 many2/*"])
def test_a_workspace_glob_is_judged_entry_by_entry(world, command):
    for flavor in HOSTS:
        out = _run(world, flavor, command)
        assert "secret-bearing path" in json.dumps(out), (flavor, command, out)


def test_a_glob_link_out_is_outside_and_plain_files_pass(world):
    out = _run(world, "claude-code", "cat many2/z*")  # 81 matches
    assert "path resolves outside" in json.dumps(out), out
    assert not _fired(_run(world, "claude-code", "cat many2/f0*"))


def test_a_glob_over_more_than_5000_entries_keeps_base_when_nothing_was_seen(world):
    # Keep-base check (the stated limit): nothing outside among 5,001 entries.
    big = world["ws"] / "big"
    big.mkdir()
    for i in range(5001):
        (big / f"f{i}").touch()
    import time

    start = time.perf_counter()
    out = _run(world, "claude-code", "cat big/*")
    assert not _fired(out), out
    assert time.perf_counter() - start < 5


def test_a_link_out_seen_before_the_bound_is_kept(world):
    # Round 3 item 1: the scan stops at 5,000 entries but keeps what it saw.
    big = world["ws"] / "bl"
    big.mkdir()
    for i in range(6000):
        (big / f"f{i}").touch()
    for n in range(50):
        link = big / f"lnk_out{n}"
        link.symlink_to("/etc/passwd")
        names = [e.name for e in os.scandir(big)]
        if names.index(link.name) < 4900:
            break
        link.unlink()
    for command in ("cat bl/*", "cat bl/lnk*"):
        out = _run(world, "claude-code", command)
        assert "path resolves outside" in json.dumps(out), (command, out)


# ------------------------------------------------------------------ item 3
@pytest.mark.parametrize("command", [
    'bash -lc "cat /etc/passwd"', "sh -ec 'cat /etc/passwd'", "bash -lc 'cd /etc; cat passwd'",
    "! cat /etc/passwd", "cat ${f:-/etc/passwd}", "cat ${f:=/etc/passwd}",
    "x=$(< /etc/passwd)", 'echo "$(< /etc/passwd)"',
    "find /etc -maxdepth 1 -type f -exec cat {} +", "find /etc -execdir head -n 1 {} \\;",
    "cat $HOME/notes.txt", "cat ${HOME}/notes.txt", "cat ~/notes.txt", "cat ~",
])
def test_forms_visible_in_the_text_are_judged(world, monkeypatch, command):
    # $HOME points into the workspace: `~` and $HOME must still mean the
    # account's home (outside), which round 1 did not do.
    monkeypatch.setenv("HOME", str(world["ws"]))
    for flavor in HOSTS:
        assert _kind(flavor, _run(world, flavor, command)) == ASK[flavor], (flavor, command)


@pytest.mark.parametrize("command", [
    "f=a.txt; cat ${f:-/etc/passwd}", "cat $PWD/a.txt", "cd sub && cat $PWD/../a.txt",
    "find . -name '*.txt' -exec cat {} +", "cat $(echo /etc/passwd)", "cat `echo /etc/passwd`",
    "xargs cat < list.txt", "python3 -c 'open(\"/etc/passwd\").read()'",
    "F=$(ls /etc/passwd); cat $F", "read f; cat $f",
])
def test_paths_inside_or_produced_at_run_time_keep_base(world, command):
    # Keep-base checks: these must not ask (they did not before either).
    for flavor in HOSTS:
        assert not _fired(_run(world, flavor, command)), (flavor, command)


def test_a_redirected_home_is_not_trusted(world, monkeypatch):
    monkeypatch.setenv("HOME", str(world["ws"]))  # $HOME pointed into the workspace
    out = _run(world, "claude-code", "cat $HOME/a.txt")  # the account's home is elsewhere
    assert "path resolves outside" in json.dumps(out), out


# ------------------------------------------------------------------ items 5, 6
@pytest.mark.parametrize("layer", ["user", "project", "local"])
def test_claude_code_allow_layers_withdraw_the_outside_ask(world, layer):
    rule = json.dumps({"permissions": {"allow": ["Bash(cat:*)"]}})
    if layer == "user":
        (world["cfg"] / "settings.json").write_text(rule)
    else:
        (world["ws"] / ".claude").mkdir()
        name = "settings.json" if layer == "project" else "settings.local.json"
        (world["ws"] / ".claude" / name).write_text(rule)
    assert _kind("claude-code", _run(world, "claude-code", "cat /etc/passwd")) == "pass"
    assert _kind("claude-code", _run(world, "claude-code", "cat /etc/passwd /etc/hosts")) == "pass"
    assert _kind("claude-code", _run(world, "claude-code", "cat /etc/passwd | head -3")) == "pass"
    assert _kind("claude-code", _run(world, "claude-code", "cat .env")) == "ask"
    assert _kind("claude-code", _run(world, "claude-code", "tail /etc/passwd")) == "ask"


def test_antigravity_allow_rule_withdraws_the_outside_ask(world):
    agy = world["home"] / ".gemini" / "antigravity-cli" / "settings.json"
    agy.parent.mkdir(parents=True)
    agy.write_text(json.dumps({"permissions": {"allow": ["command(cat)"]}}))
    out = _run(world, "antigravity", "cat /etc/passwd")
    assert out["decision"] == "allow" and not _fired(out), out
    assert _kind("antigravity", _run(world, "antigravity", "cat .env")) == "ask"
    assert _kind("antigravity", _run(world, "antigravity", "tail /etc/passwd")) == "ask"


# ------------------------------------------------------------------ item 7
@pytest.mark.parametrize("command", [
    "(cd /tmp && ls); cat a.txt", "(cd /etc); cat a.txt", "echo $(cd /etc && pwd); cat a.txt",
    "cd /etc | cat a.txt",
    "for f in a.txt; do cat $f; done; for f in /etc/passwd; do echo $f; done",
    "cat /dev/./null", "cat /dev//null < /dev/./null",
    "cd {OUT} 2>/dev/null; F=$(find / -name x.log | head -1); grep -n pat \"$F\"",
    "cd {OUT}; F=`ls`; cat $F",
])
def test_scoped_cd_positional_bindings_and_run_time_paths_do_not_ask(world, command):
    command = command.replace("{OUT}", str(world["outside"]))
    for flavor in HOSTS:
        assert not _fired(_run(world, flavor, command)), (flavor, command)


def test_a_binding_applies_from_its_own_position(world):
    # Regression guard (held in round 1 too); the false ask round 2 fixed is
    # in test_scoped_cd_positional_bindings_and_run_time_paths_do_not_ask.
    command = "for f in /etc/passwd; do echo $f; done; for f in a.txt; do cat $f; done"
    assert not _fired(_run(world, "claude-code", command))
    command = "for f in a.txt; do echo $f; done; for f in /etc/passwd; do cat $f; done"
    assert _kind("claude-code", _run(world, "claude-code", command)) == "ask"


# ------------------------------------------------------------------ item 10
@pytest.mark.parametrize("which", ["home", "tmp", "parent", "shallow"])
def test_a_scratchpad_that_covers_the_workspace_or_home_is_ignored(world, which):
    target = {"home": world["home"], "tmp": world["tmp"], "parent": world["ws"].parent,
              "shallow": world["tmp"]}[which]
    if which == "shallow":
        target = type(world["tmp"])("/tmp")
    f = world["outside"] / "x.txt"
    f.write_text("x\n")
    path = str(f) if which != "home" else str(world["home"] / "notes.txt")
    out = _emit("claude-code", _payload("claude-code", world, path=path,
                                        scratchpad_dir=str(target)))
    assert _kind("claude-code", out) == "ask", (which, out)


def test_a_proper_scratchpad_is_trusted(world):
    # Keep-base check: the stricter scratchpad test is the parametrised one above.
    f = world["outside"] / "x.txt"
    f.write_text("x\n")
    for flavor in ("claude-code", "codex"):
        out = _emit(flavor, _payload(flavor, world, path=str(f),
                                     scratchpad_dir=str(world["outside"])))
        assert _kind(flavor, out) == "pass", out
        out = _run(world, flavor, f"cat {f}", scratchpad_dir=str(world["outside"]))
        assert _kind(flavor, out) == "pass", out



# ------------------------------------------------------------------ round 3
@pytest.mark.parametrize("command", [
    "f=/etc/passwd; for g in $f; do cat $g; done", "cat ${a:-${b:-/etc/passwd}}",
    "cat ${a:+/etc/passwd}", "a=; cat ${a:-/etc/passwd}", 'cat <<< "$(< /etc/passwd)"',
    "find . -exec cat /etc/passwd {} +", "find . -exec head -n 1 /etc/passwd {} \\;",
])
def test_round3_forms_are_judged(world, command):
    for flavor in HOSTS:
        assert _kind(flavor, _run(world, flavor, command)) == ASK[flavor], (flavor, command)


@pytest.mark.parametrize("command", [
    "f=a.txt; for g in $f; do cat $g; done", "a=x.txt; cat ${a:-/etc/passwd}",
    "cd /etc & cat a.txt", "find . -exec cat {} +",
])
def test_round3_forms_that_keep_base(world, command):
    # Keep-base checks.
    for flavor in HOSTS:
        assert not _fired(_run(world, flavor, command)), (flavor, command)


@pytest.mark.parametrize("name", [".npmrc.dist", ".netrc.defaults"])
def test_dist_and_defaults_copies_are_credential_files(world, name):
    # Fails on 776c57d, which exempted these suffixes as templates (08aeb79
    # had no template exemption at all).
    (world["ws"] / name).write_text("x\n")
    for flavor in HOSTS:
        assert "secret-bearing path" in json.dumps(_run(world, flavor, f"cat {name}"))


def test_antigravity_honours_exactly_base_allow_forms(world):
    agy = world["home"] / ".gemini" / "antigravity-cli" / "settings.json"
    agy.parent.mkdir(parents=True)
    agy.write_text(json.dumps({"permissions": {"allow": ["command(cat *)"]}}))
    # Keep-safe check: `cat *` is not a form base's agyperm proves, so the
    # ask stands (it did on 776c57d too); `cat` withdraws it.
    assert _kind("antigravity", _run(world, "antigravity", "cat /etc/passwd")) == "ask"
    agy.write_text(json.dumps({"permissions": {"allow": ["command(cat)"]}}))
    assert _kind("antigravity", _run(world, "antigravity", "cat /etc/passwd")) == "pass"
