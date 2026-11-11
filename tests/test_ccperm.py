"""A Claude Code rewrite never makes a command more permitted than the user's
own rules make it (ctx.ccperm, ctx.hook._claude_permission_gate).

Observed live on Claude Code 2.1.322 with a stand-in hook rewriting
`cat notes.txt` (scratch liveprobe xA..xF):

  xA  deny Bash(cat:*), no hook                    -> blocked
  xB  deny Bash(cat:*), hook allow + updatedInput  -> RAN (deny bypassed)
  xC  deny Bash(cat:*), hook updatedInput only     -> blocked (rewritten form)
  xD  no rules, hook allow + updatedInput          -> ran, no approval
  xE0 no rules, hook updatedInput only             -> refused, needs approval
  xE1 allow Bash(cat:*), updatedInput only         -> refused (judged on wrapper)
  xE2 allow Bash(mywrap:*), updatedInput only      -> ran
  xF  ask Bash(cat:*), hook allow + updatedInput   -> RAN without asking

The adversarial set below crosses rule sets with commands built from the
grammar's hard cases and asserts the hook never answers "allow" where the
documented semantics give deny or ask, nor where the original is not proven
allowed.
"""

from __future__ import annotations

import io
import itertools
import json
import sys

import pytest

from ctx import ccperm, hook


def _verdict(command, rules, mode="default", extra_input=None):
    loaded = {"deny": [], "ask": [], "allow": [], "unknown": False, "reasons": []}
    for kind, vals in rules.items():
        loaded[kind] = list(vals)
    payload = {"permission_mode": mode, "cwd": "/w", "tool_input": {"command": command, **(extra_input or {})}}
    return ccperm.rewrite_verdict(command, payload, "/w", rules=loaded)[0]


# ------------------------------------------------- the live observations
LIVE = [  # (settings permissions, ccperm's answer for `cat notes.txt`: refusal only)
    ({"deny": ["Bash(cat:*)"]}, "deny"),        # xA/xB/xC: the deny must hold
    ({"ask": ["Bash(cat:*)"]}, "ask"),          # xF: the ask must hold
    ({}, "none"),                               # never an approval (review of 9d02cb4)
    ({"allow": ["Bash(cat:*)"]}, "none"),
    ({"allow": ["Bash(mywrap:*)"]}, "none"),
]


@pytest.mark.parametrize("perms,want", LIVE)
def test_live_cases(perms, want):
    assert _verdict("cat notes.txt", perms) == want


def test_live_shapes_through_the_real_hook(tmp_path, monkeypatch):
    """The emitted JSON for the observed cases: no allow past a deny or ask,
    and no decision at all where nothing is proven."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("x\n" * 20000)

    def run(command, perms):
        (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": perms}))
        payload = {"tool_name": "Bash", "cwd": str(ws), "permission_mode": "default",
                   "session_id": "s", "tool_input": {"command": command}}
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
        try:
            hook.main_pre_tool_use("claude-code")
            return json.loads(sys.stdout.getvalue()).get("hookSpecificOutput", {})
        finally:
            sys.stdin, sys.stdout = stdin, stdout

    # Default steering: every shell command leaves exactly as it arrived.
    for perms in ({"deny": ["Bash(cat:*)"]}, {"ask": ["Bash(cat:*)"]}, {}, {"allow": ["Bash(cat:*)"]}):
        assert run("cat notes.txt", perms) == {}, perms
    assert run("pytest -q", {"allow": ["Bash(pytest *)"]}) == {}
    # Opt-in rewriting: refusal from visible rules, else a rewrite with NO decision.
    (ws / "ctx.toml").write_text('version = 1\n[guard]\nsteering = "rewrite"\n')
    assert run("cat notes.txt", {"deny": ["Bash(cat:*)"]})["permissionDecision"] == "deny"
    asked = run("cat notes.txt", {"ask": ["Bash(cat:*)"]})
    assert asked["permissionDecision"] == "ask" and "updatedInput" not in asked
    for perms in ({}, {"allow": ["Bash(pytest *)"]}):
        rw = run("pytest -q", perms)
        assert "permissionDecision" not in rw and "ctx run" in rw["updatedInput"]["command"]


# ------------------------------------------------- adversarial generation
PROGRAMS = {"cat": "cat notes.txt", "rm": "rm -rf build", "git": "git push origin main",
            "curl": "curl https://example.test", "pytest": "pytest -q", "ls": "ls -la"}
#: (template, does the documented deny/ask matching reach the core?)
FORMS = [
    ("{c}", True), ("timeout 5 {c}", True), ("nohup {c}", True), ("time {c}", True),
    ("FOO=1 {c}", True), ("xargs {c}", True), ("echo hi && {c}", True), ("{c} || true", True),
    ("{c}; echo done", True), ("{c} | head -5", True), ("echo $({c})", True), ("( {c} )", True),
    ("echo `{c}`", True), ("{c}\nls", True), ("{c} & wait", True), ("{c} # trailing", True),
    ("{c} > out.txt", True), ("command {c}", True), ("nice -n 5 {c}", True),
    ("bash -c '{c}'", False), ("/usr/bin/{c}", False),
]
RULESETS = []
for prog, core in PROGRAMS.items():
    word = core.split()[0]
    for kind in ("deny", "ask"):
        for spec in (f"Bash({word}:*)", f"Bash({word} *)", f"Bash({core})", "Bash", "Bash(*)",
                     f"Bash({' '.join(core.split()[:2])} *)", "*"):
            RULESETS.append((prog, {kind: [spec], "allow": ["Bash(ls *)", f"Bash({word} *)"]}))
RULESETS.append(("cat", {"deny": ["Read(./notes.txt)"]}))
RULESETS.append(("cat", {"ask": ["Read(**/notes.txt)"]}))
RULESETS.append(("rm", {"allow": ["Bash(rm *)", "Bash"], "deny": ["Bash(rm -rf *)"]}))


def _oracle(prog, perms, form_reaches):
    """What the documentation says for the CORE subcommand."""
    core = PROGRAMS[prog]
    for kind in ("deny", "ask"):
        for rule in perms.get(kind, []):
            tool, spec = ccperm.parse_rule(rule)
            if tool in ("Bash", "*") and (spec is None or not form_reaches
                                          or ccperm.bash_spec_matches(spec, core)):
                if spec is None or form_reaches:
                    return kind
            if tool == "Read" and prog == "cat":
                return kind
    return None


CASES = [(prog, perms, form, reaches) for (prog, perms), (form, reaches)
         in itertools.product(RULESETS, FORMS)]


def test_adversarial_set_is_large():
    assert len(CASES) >= 1500


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "dontAsk", "plan"])
def test_never_allow_where_the_documented_semantics_give_deny_or_ask(mode):
    wrong = []
    for prog, perms, form, reaches in CASES:
        command = form.format(c=PROGRAMS[prog])
        got = _verdict(command, perms, mode)
        want = _oracle(prog, perms, reaches)
        if want and got == "allow":
            wrong.append((command, perms, got, want))
        if want == "deny" and reaches and got not in ("deny",):
            wrong.append((command, perms, got, want))
    assert not wrong, wrong[:10]


def test_allow_only_for_plain_proven_commands():
    """Whatever the rules, a command with a shell construct, a wrapper or an
    assignment is never proven: the host decides it."""
    for prog, perms, form, _ in CASES:
        command = form.format(c=PROGRAMS[prog])
        if _verdict(command, perms) == "allow":
            assert ccperm.strict_segments(command) is not None, (command, perms)


def test_beyond_the_documented_matching_errs_toward_deny():
    """The adversarial run found three cases the documented matching leaves
    to the host and the wrapper would have carried past the rule: a Read rule
    on a file read inside ``bash -c``, and an absolute-path program under an
    allow-all plus a deny on the bare name. They now deny or ask."""
    assert _verdict("bash -c 'cat notes.txt'", {"deny": ["Read(./notes.txt)"]}) == "deny"
    assert _verdict("bash -c 'cat notes.txt'", {"ask": ["Read(**/notes.txt)"]}) == "ask"
    assert _verdict("/usr/bin/rm -rf build",
                    {"allow": ["Bash"], "deny": ["Bash(rm -rf *)"]}) == "deny"
    assert _verdict("sh -lc 'git push origin main'", {"ask": ["Bash(git push *)"]}) == "ask"


def test_no_adversarial_case_is_left_to_the_host_where_the_docs_deny_or_ask():
    left = [(form, perms) for prog, perms, form, reaches in CASES
            if _oracle(prog, perms, reaches)
            and _verdict(form.format(c=PROGRAMS[prog]), perms) not in ("deny", "ask")]
    assert not left, left[:5]


@pytest.mark.parametrize("command", [
    "cat notes.txt", "git status", "ls ../secrets", "cat /etc/passwd", "cat ~/notes",
])
def test_unknown_sources_never_allow(command):
    rules = {"deny": [], "ask": [], "allow": ["Bash"], "unknown": True, "reasons": ["x"]}
    payload = {"permission_mode": "default", "cwd": "/w", "tool_input": {"command": command}}
    assert ccperm.rewrite_verdict(command, payload, "/w", rules=rules)[0] != "allow"


def test_reads_outside_the_working_directory_are_not_proven_read_only():
    for command in ("cat /etc/passwd", "cat ../x", "head ~/.bashrc"):
        assert _verdict(command, {}) == "none", command


def test_parameter_rules_match_the_input_field():
    assert _verdict("sleep 1", {"deny": ["Bash(run_in_background:true)"]},
                    extra_input={"run_in_background": True}) == "deny"
    assert _verdict("sleep 1", {"deny": ["Bash(run_in_background:true)"]}) != "deny"


def test_disallowed_tools_on_the_launch_command_line_are_deny_rules():
    argvs = [["sh", "-c", "ctx hook"], ["node", "/usr/bin/claude", "-p", "x",
                                         "--disallowedTools", "Bash(cat:*)", "Grep"]]
    docs, deny, unknown = ccperm._cli_rules(argvs)
    assert "Bash(cat:*)" in deny and not unknown
    assert ccperm._cli_rules(None)[2] is True  # unreadable command line: unknown
    _d, _x, unknown = ccperm._cli_rules([["claude", "--setting-sources", "project"]])
    assert unknown


def test_wholesale_wrapper_allow_is_reported(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ctx:*)", "Bash(npm test)"]}}))
    monkeypatch.setenv("HOME", str(home))
    found = ccperm.wholesale_wrapper_allows(str(tmp_path / "ws"))
    assert len(found) == 1 and found[0].endswith("Bash(ctx:*)")
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash", "Bash(ctx run *)", "Bash(ctx get *)"]}}))
    found = ccperm.wholesale_wrapper_allows(str(tmp_path / "ws"))
    assert [f.rsplit(": ", 1)[1] for f in found] == ["Bash", "Bash(ctx run *)"]


def test_doctor_reports_wholesale_wrapper_allow(tmp_path, monkeypatch):
    from ctx.installer import doctor_checks
    from ctx.workspace import resolve_workspace

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    ws = resolve_workspace(str(ws_dir))

    def approval():
        return next(c for c in doctor_checks(ws) if c[0] == "claude code approval")

    ok = approval()
    assert ok[1] and "exactly as without ctx" in ok[2] and "never a rewritten call" in ok[2]
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ctx:*)"]}}))
    bad = approval()
    assert not bad[1] and "ANY command" in bad[2] and "Bash(ctx:*)" in bad[2]
    assert str(home) not in bad[2]


# ------------------------------------------- one choke point for every answer
def test_every_claude_code_answer_passes_the_choke_point(tmp_path, monkeypatch):
    """Code that later builds an `allow` (with advice, for any tool) still
    leaves with no decision: claude_code_pre_tool_use_output strips it."""
    def later_code(decision):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "permissionDecisionReason": "x", "additionalContext": "advice"}}

    monkeypatch.setattr(hook, "_to_claude_code_schema", later_code)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    payload = {"tool_name": "Read", "cwd": str(tmp_path), "session_id": "s",
               "tool_input": {"file_path": str(tmp_path / "a.txt")}}
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use("claude-code")
        out = json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    assert out == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "advice"}}


def test_choke_point_keeps_only_proven_allows():
    allow = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    assert hook.claude_code_pre_tool_use_output(allow, {"decision": "allow"}) == {}
    assert hook.claude_code_pre_tool_use_output(allow, {"_permission": "allow"}) == allow
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}
    assert hook.claude_code_pre_tool_use_output(deny, {}) == deny


@pytest.mark.parametrize("payload,approved", [
    ({"tool_name": "Bash", "tool_input": {"command": "ctx get run:abc123"}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "ctx search foo"}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "ctx run -- ls"}}, False),
    ({"tool_name": "Bash", "tool_input": {"command": "ctx get run:abc; rm -rf x"}}, False),
    ({"tool_name": "Bash", "tool_input": {"command": "ctx get run:abc\nrm -rf x"}}, False),
    ({"tool_name": "Bash", "tool_input": {"command": "./ctx get run:abc"}}, False),
    ({"tool_name": "mcp__ctx__ctx", "tool_input": {"op": "stats"}}, True),
    ({"tool_name": "mcp__ctx__ctx", "tool_input": {"op": "run", "command": "rm -rf x"}}, False),
    ({"tool_name": "Read", "tool_input": {"file_path": "x"}}, False),
])
def test_ctx_self_approves_exactly_its_read_only_verbs(tmp_path, payload, approved):
    payload = {**payload, "cwd": str(tmp_path)}
    assert hook._ctx_self_approved(payload, str(tmp_path)) is approved



# --------------------------- installing ctx adds no prompt to a shell command
#: Observed live on 2.1.322, default mode, no rules, NO hook installed:
#: True = ran without asking, False = refused as needing approval.
HOST_RUNS_WITHOUT_ASKING = {
    "ls": True, "cat f.txt": True, "head -5 f.txt": True, "git status": True, "git diff": True,
    "git log -3": True, "grep -rn x .": True, "find . -name '*.py'": True, "wc -l f.txt": True,
    "sed -n 1,5p f.txt": True, "tail -3 f.txt": True, "stat f.txt": True, "du -a": True,
    "diff f.txt a.py": True, "which ls": True, "echo hi": True, "pwd": True,
    "git show --stat": True, "cat ../outside.txt": False, "find . -name a.py -delete": False,
}


@pytest.mark.parametrize("command,host_runs", sorted(HOST_RUNS_WITHOUT_ASKING.items()))
def test_ctx_prompts_exactly_where_the_host_does(tmp_path, monkeypatch, command, host_runs):
    """With ctx installed a command prompts exactly when it would without
    ctx: the hook's answer is either a proven allow (only for a command the
    host runs without asking) or nothing at all."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ctx.toml").write_text("version = 1\n")
    (ws / "f.txt").write_text("x\n" * 40000)
    (ws / "a.py").write_text("x = 1\n")
    payload = {"tool_name": "Bash", "cwd": str(ws), "permission_mode": "default",
               "session_id": "s", "tool_input": {"command": command}}
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use("claude-code")
        out = json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    hso = out.get("hookSpecificOutput", {})
    decision = hso.get("permissionDecision")
    assert decision in (None, "allow"), (command, out)
    if decision == "allow":
        assert host_runs, f"ctx approved {command!r}, which the host asks about"
        assert "updatedInput" in hso
    else:
        assert "updatedInput" not in hso, (command, out)  # never a rewrite without allow


def test_read_tool_narrowing_is_unaffected(tmp_path, monkeypatch):
    """A narrowed Read (limit) keeps the same file_path: the host's rule sees
    the same file, so it carries no decision and needs none."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    gated = hook._claude_permission_gate(
        {"decision": "allow", "rewrite": {"updatedInput": {"file_path": "/w/f", "limit": 200}}},
        {"tool_name": "Read", "tool_input": {"file_path": "/w/f"}}, str(tmp_path))
    assert gated["_permission"] == "none" and gated["rewrite"]["updatedInput"]["file_path"] == "/w/f"


def test_reads_turned_away_with_advice_are_unaffected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    deny = {"decision": "deny", "reason": "CTX_CONTEXT_GUARD: use ctx get"}
    assert hook._claude_permission_gate(deny, {"tool_name": "Bash", "tool_input": {
        "command": "tail -f app.log"}}, str(tmp_path)) == deny


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "dontAsk", "plan"])
def test_adversarial_answers_under_the_rewrite_with_allow_rule(mode):
    """What Claude Code receives for each adversarial case: a rewrite with a
    proven allow, or the command untouched. Never a rewrite without allow,
    and never an allow where the documented semantics give deny or ask."""
    for prog, perms, form, reaches in CASES:
        command = form.format(c=PROGRAMS[prog])
        verdict = _verdict(command, perms, mode)
        emitted = "rewrite+allow" if verdict == "allow" else "untouched"
        if _oracle(prog, perms, reaches):
            assert emitted == "untouched", (command, perms)
