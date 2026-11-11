"""Adversarial tests for ctx.agyperm: never ``allow`` where agy would ask or deny.

The cases are generated from the hard cases of agy's documented permission
grammar (https://antigravity.google/docs/permissions/, saved 2026-11-11):

* Deny > Ask > Allow;
* command or process substitution, backticks, arithmetic, brace expansion,
  non-literal command names, descriptor or network redirections and
  exec-capable tool flags (``git -c``, ``tar --to-command``) disable prefix
  matching: only a full-line match allows, otherwise agy asks;
* ``timeout``, ``nohup``, ``nice`` and ``env`` are transparent wrappers whose
  inner command is evaluated on its own, and pipelines and ``&&``/``||``/``;``
  chains are evaluated per command.

Each family below is crossed with rule sets that include a rule approving
``ctx`` (so the rewrite itself would be approved, which is when the module's
answer matters), and the documented outcome is known by construction.
"""

from __future__ import annotations

import itertools
import json
import os

import pytest

from ctx import agyperm

PROGRAMS = ["rm", "cat", "git", "python3", "curl"]
ARGS = ["x.txt", "-rf build", "big.log other.log", ""]
CONSTRUCTS = [
    "{p} {a} $(touch marker)", "{p} {a} `touch marker`", "{p} <(touch marker) {a}",
    "{p} $((1+2)) {a}", "{p} {{a,b}}.txt {a}", "$PROG {a}", "{p} {a} >&3",
    "{p} {a} 2>&1", "{p} {a} > /dev/tcp/127.0.0.1/9", "{p} {a} $(echo $(id))",
    "{p} '{a}' \"$(touch marker)\"", "git -c core.pager=touch log {a}",
    "tar --to-command=touch -xf a.tar {a}", "{p} {a} | sh", "{p} {a} & touch marker",
    "{p} {a}; touch marker", "{p}\ntouch marker", "{p} {a} *.log", "A=1 {p} {a}",
    "{p} {a} ~/x", "{p} {a} \\; touch marker",
]
WRAPPERS = ["timeout 5 {c}", "nohup {c}", "nice -n 5 {c}", "env A=1 {c}",
            "timeout 5 nice {c}", "env -i nohup {c}", "/usr/bin/timeout 5 {c}"]
CHAINS = ["echo hi && {c}", "echo hi || {c}", "echo hi; {c}", "echo hi | {c}",
          "echo hi & {c}", "echo hi\n{c}", "({c})", "{{ {c}; }}", "echo $({c})",
          "echo `{c}`", "true && echo hi | {c}"]


def _home(tmp_path, *, allow=(), deny=(), ask=(), project_allow=()):
    home = tmp_path / "home"
    cli = home / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True, exist_ok=True)
    (cli / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": list(allow), "deny": list(deny), "ask": list(ask)}}))
    if project_allow:
        proj = home / ".gemini" / "config" / "projects"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "other-project.json").write_text(json.dumps(
            {"permissions": {"allow": list(project_allow)}}))
    return str(home)


def _rewritten(cmd: str) -> str:
    return "ctx run --shell -- '" + cmd.replace("'", "'\\''") + "'"


def _construct_cases():
    for tpl, p, a in itertools.product(CONSTRUCTS, PROGRAMS, ARGS):
        yield " ".join(tpl.format(p=p, a=a).split(" ")).strip()


@pytest.fixture(autouse=True)
def _no_ancestor_settings(monkeypatch):
    monkeypatch.setattr(agyperm, "_inline_settings", lambda: False)


# ---------------------------------------------------------- the families
ALLOW_SETS = [
    [f"command({p})" for p in PROGRAMS] + ["command(ctx)", "command(echo)", "command(true)"],
    ["command(*)"],
    ["command(ctx)", "command(regex:.*)"] + [f"command(regex:{p} .*)" for p in PROGRAMS],
    ["command(ctx run)", "command(git log)", "command(cat x.txt)"],
]


def test_constructs_never_allow_without_a_full_line_rule(tmp_path):
    n = 0
    for i, allow in enumerate(ALLOW_SETS):
        home = _home(tmp_path / str(i), allow=allow)
        for cmd in set(_construct_cases()):
            verdict, _ = agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)
            assert verdict != "allow", (allow, cmd)
            n += 1
    assert n > 1400


def test_a_full_line_literal_rule_does_allow_a_construct(tmp_path):
    cmd = "cat big.log $(touch marker)"
    home = _home(tmp_path, allow=["command(ctx)", f"command({cmd})"])
    assert agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)[0] == "allow"


@pytest.mark.parametrize("deny_form", ["command({p})", "command(regex:{p})", "command({p} {a1})"])
def test_a_deny_rule_is_honoured_through_wrappers_and_chains(tmp_path, deny_form):
    n = 0
    for p, a in itertools.product(PROGRAMS, ["x.txt", "-rf build"]):
        a1 = a.split()[0]
        deny = [deny_form.format(p=p, a1=a1)]
        home = _home(tmp_path / f"{p}{a1}", allow=["command(*)", "command(ctx)"], deny=deny)
        inner = f"{p} {a}"
        cases = [w.format(c=inner) for w in WRAPPERS] + [c.format(c=inner) for c in CHAINS]
        cases += [w.format(c=c.format(c=inner)) for w in WRAPPERS[:2] for c in CHAINS[:3]]
        for cmd in cases:
            verdict, _ = agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)
            assert verdict == "deny", (deny, cmd, verdict)
            n += 1
    assert n >= 200


def test_an_ask_rule_is_honoured_through_wrappers_and_chains(tmp_path):
    for p in PROGRAMS:
        home = _home(tmp_path / p, allow=["command(*)", "command(ctx)"], ask=[f"command({p})"])
        inner = f"{p} x.txt"
        for cmd in [w.format(c=inner) for w in WRAPPERS] + [c.format(c=inner) for c in CHAINS]:
            assert agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)[0] == "force_ask", cmd


def test_wrappers_and_chains_are_never_proven_allowed(tmp_path):
    """Even with every program allowed, a wrapped or chained line is asked:
    the module only proves a single plain command."""
    allow = [f"command({p})" for p in PROGRAMS + ["timeout", "nohup", "nice", "env", "echo"]]
    home = _home(tmp_path, allow=allow + ["command(ctx)"])
    for p in PROGRAMS:
        for cmd in [w.format(c=f"{p} x") for w in WRAPPERS] + [c.format(c=f"{p} x") for c in CHAINS]:
            assert agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)[0] == "force_ask", cmd


def test_options_and_odd_words_are_asked(tmp_path):
    home = _home(tmp_path, allow=["command(ctx)", "command(git)", "command(pytest)", "command(cat)"])
    for cmd in ["git -c core.pager=touch log", "pytest -q", "cat 'a b'", "cat a\\ b",
                "cat ~/x", "cat x*", "cat x?", "cat [ab]", "git log --format=%H",
                "cat #comment", "cat !$"]:
        assert agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)[0] == "force_ask", cmd


def test_plain_commands_are_proven(tmp_path):
    home = _home(tmp_path, allow=["command(ctx)", "command(cat)", "command(git log)",
                                  "command(regex:python3 [a-z]+\\.py)"])
    for cmd in ["cat big.log", "cat a.log b.log", "git log", "git log main",
                "python3 diagnose.py", "/bin/cat big.log"]:
        verdict = agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)[0]
        assert verdict == ("force_ask" if cmd.startswith("/bin/") else "allow"), cmd
    # a regex rule proves only a command with exactly its token count
    assert agyperm.rewrite_verdict("python3 diagnose.py x", "ctx run", home)[0] == "force_ask"
    # a literal rule is word-by-word, not a string prefix
    assert agyperm.rewrite_verdict("git logx", "ctx run", home)[0] == "force_ask"


def test_without_a_rule_that_could_approve_the_rewrite_the_host_asks_itself(tmp_path):
    home = _home(tmp_path, allow=["command(echo)"])
    for cmd in ["cat big.log $(touch marker)", "timeout 5 cat x"]:
        assert agyperm.rewrite_verdict(cmd, _rewritten(cmd), home)[0] == "allow"
    # but a rule naming any word inside the rewrite counts as approving it
    home = _home(tmp_path / "b", allow=["command(cat)"])
    assert agyperm.rewrite_verdict("cat x $(id)", _rewritten("cat x $(id)"), home)[0] == "force_ask"


def test_another_projects_allow_rule_proves_nothing(tmp_path):
    home = _home(tmp_path, allow=["command(ctx)"], project_allow=["command(cat)"])
    assert agyperm.rewrite_verdict("cat x", _rewritten("cat x"), home)[0] == "force_ask"


def test_project_deny_rules_still_apply(tmp_path):
    home = _home(tmp_path, allow=["command(ctx)", "command(cat)"])
    proj = os.path.join(home, ".gemini", "config", "projects")
    os.makedirs(proj)
    with open(os.path.join(proj, "p.json"), "w") as fh:
        json.dump({"permissions": {"deny": ["command(cat)"]}}, fh)
    assert agyperm.rewrite_verdict("cat x", _rewritten("cat x"), home)[0] == "deny"


@pytest.mark.parametrize("settings", [
    "{not json", json.dumps({"permissions": {"allow": ["command(ctx)", "command(cat)",
                                                       "unsandboxed(cat)"]}}),
    json.dumps({"permissions": {"allow": ["command(ctx)", "command(cat)", "bogus"]}}),
    json.dumps({"permissions": ["command(cat)"]}),
    json.dumps({"permissions": {"allow": "command(cat)"}}),
])
def test_any_source_it_cannot_read_asks(tmp_path, settings):
    home = tmp_path / "home" / ".gemini" / "antigravity-cli"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(settings)
    verdict = agyperm.rewrite_verdict("cat x", _rewritten("cat x"), str(tmp_path / "home"))[0]
    assert verdict == "force_ask"


def test_inline_settings_at_launch_asks(tmp_path, monkeypatch):
    home = _home(tmp_path, allow=["command(ctx)", "command(cat)"])
    monkeypatch.setattr(agyperm, "_inline_settings", lambda: True)
    assert agyperm.rewrite_verdict("cat x", _rewritten("cat x"), home)[0] == "force_ask"


def test_no_home_asks():
    assert agyperm.rewrite_verdict("cat x", "ctx run -- cat x", "")[0] == "force_ask"


def test_bounds(tmp_path):
    import time

    home = _home(tmp_path, allow=["command(ctx)", "command(cat)"])
    t = time.perf_counter()
    for size in (agyperm.MAX_COMMAND + 1, 300_000, 5_000_000):
        assert agyperm.rewrite_verdict("cat " + "a" * size, "ctx run", home)[0] == "force_ask"
    assert time.perf_counter() - t < 1.0
    big = tmp_path / "big" / ".gemini" / "antigravity-cli"
    big.mkdir(parents=True)
    (big / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["command(ctx)", "command(cat)"]}, "pad": "x" * agyperm.MAX_RULES_FILE}))
    t = time.perf_counter()
    assert agyperm.rewrite_verdict("cat x", "ctx run -- cat x", str(tmp_path / "big"))[0] == "force_ask"
    assert time.perf_counter() - t < 0.5


def test_inline_settings_detection_reads_the_ancestors(monkeypatch):
    # the real function, on this process tree: pytest was not launched with --settings
    monkeypatch.undo()
    assert agyperm._inline_settings() is False
