"""Two contracts a bug bash found broken (evals/devex/, 2026-08-02).

1. An allow-shortcut may answer the VOLUME question but never the SAFETY one.
2. Malformed foreign config is refused, never raised.

Both are class-level: each covered several confirmed defects that shared one
root, so the fix is a shared mechanism rather than a patch per site.
"""

from __future__ import annotations

import json

from ctx.hook import classify_command
from ctx.installer import _hook_command_present, _iter_hook_commands


def _policy(**over):
    p = {"deny_commands": ["rm -rf", "curl"], "allow_commands": [],
         "steering": "deny"}
    p.update(over)
    return p


# ------------------------------------------------- safety vs volume classes
def test_redirect_does_not_bypass_committed_deny_commands():
    """`denied > out.txt 2>&1` used to return allow before deny_commands was
    ever consulted -- a rule the repo committed on purpose, switched off by
    adding a `>`."""
    p = _policy()
    for cmd in ("rm -rf /tmp/x > out.txt 2>&1",
                "rm -rf /tmp/x >> out.txt 2>&1",
                "curl http://evil &> out.txt"):
        d = classify_command(cmd, p)
        assert d["decision"] != "allow", f"{cmd!r} bypassed a safety rule"


def test_redirect_still_satisfies_volume_class_steering():
    """The shortcut must keep working for what it is FOR. A redirect genuinely
    solves the volume problem, so volume-class steering stays bypassable --
    only the safety class does not."""
    d = classify_command("pytest > out.log 2>&1", _policy())
    assert d["decision"] == "allow"


def test_redirect_to_pseudo_device_is_not_a_shortcut():
    d = classify_command("rm -rf /tmp/x > /dev/null 2>&1", _policy())
    assert d["decision"] != "allow"


# --------------------------------------------- malformed foreign settings
def test_hook_present_tolerates_every_malformed_shape():
    """The traversal made three unchecked shape assumptions; any of them
    raised where the install path documents a graceful refusal."""
    for bad in (
        {"hooks": []},                                   # list, not dict
        {"hooks": {"PreToolUse": {}}},                   # dict, not list
        {"hooks": {"PreToolUse": ["nope"]}},             # str group
        {"hooks": {"PreToolUse": [{"hooks": "nope"}]}},  # str entries
        {"hooks": {"PreToolUse": [{"hooks": [None]}]}},  # null hook
        {"hooks": None},
        {},
        [],
        None,
        "not-a-document",
    ):
        assert _hook_command_present(bad, "ctx") is False
        assert list(_iter_hook_commands(bad)) == []


def test_hook_present_still_finds_a_real_entry():
    good = {"hooks": {"PreToolUse": [
        {"hooks": [{"command": "/usr/bin/ctx hook claude-code"}]}
    ]}}
    assert _hook_command_present(good, "ctx") is True


def test_ephemeral_wrap_refuses_malformed_settings(tmp_path, capsys):
    """The persistent install path refuses a malformed settings.json by name;
    the ephemeral path read the same file with a bare json.loads and died of
    an unhandled JSONDecodeError. One file, one reader."""
    from ctx.wrap import _wrap_claude_merged

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{ not json", encoding="utf-8")
    rc = _wrap_claude_merged(
        tmp_path, {"hooks": {"PreToolUse": [{"hooks": []}]}}, "claude", []
    )
    assert rc == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_ephemeral_wrap_refuses_wrong_hooks_shape(tmp_path, capsys):
    from ctx.wrap import _wrap_claude_merged

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": ["wrong shape"]}), encoding="utf-8"
    )
    rc = _wrap_claude_merged(
        tmp_path, {"hooks": {"PreToolUse": [{"hooks": []}]}}, "claude", []
    )
    assert rc == 2
    assert "not an object" in capsys.readouterr().err


# ------------------------------------------- secret guard: one door was open
def test_secret_paths_force_ask_through_the_shell_too():
    """docs/TROUBLESHOOTING.md promises a blanket guarantee, but the check
    lived only in classify_read -- the native Read door. `head .env` and
    friends walked past it through Bash."""
    p = _policy(deny_commands=[])
    for cmd in ("head .env", "cat secrets.json", "tail -n 5 id_rsa",
                "cat app-credentials.yaml", "head ~/.aws/config"):
        assert classify_command(cmd, p)["decision"] == "force_ask", cmd


def test_secret_guard_does_not_fire_on_ordinary_words_or_outrank_deny():
    """Two errors the first cut made: it matched the bare WORD `secrets` in a
    sentence, and being placed ahead of deny_commands it downgraded an
    explicit repo-committed deny into a weaker force_ask."""
    p = _policy(deny_commands=["echo secrets"])
    assert classify_command("echo secrets please", p)["decision"] == "deny"
    assert classify_command("echo hello", p)["decision"] == "allow"
    assert classify_command("head README.md", p)["decision"] == "allow"
