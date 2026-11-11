"""Acceptance: the governance table, its registrations, and the coverage ledger.

"Prefer ctx for everything" is a claim about every tool call, so it is a
declared table (ctx.governance) that the hook reads, that the static hook
registrations are pinned to, and that `ctx coverage` prints. These tests hold
the three together and pin the defects that motivated them:

a. MCP tools were classified by the words in their names: on every host whose
   pre-hook sees MCP calls, `mcp__github__list_issues` was denied as a native
   "search" and `mcp__fs__read_file` had a `limit` injected into its args.
b. Claude Code's PostToolUse matcher named the sub-agent tool `Task`; the tool
   is `Agent`, so sub-agent results skipped the output gate.
c. The same matcher omitted the edit tools, so the edit-outcome ledger never
   fired for Claude Code, and edit failures (PostToolUseFailure) never could.
d. Notebook reads, listings, background-output readers, the skill tool and
   the tool-search tool had no explicit decision.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ctx import governance as gov
from ctx import hook

ROOT = Path(__file__).resolve().parent.parent

# Claude Code tools reference (code.claude.com/docs/en/tools-reference), read
# 2026-11-10 against CLI 2.1.322. Every one must have an explicit row.
CLAUDE_TOOLS_REFERENCE = (
    "Agent", "Artifact", "AskUserQuestion", "Bash", "CronCreate", "CronDelete",
    "CronList", "Edit", "EndConversation", "EnterPlanMode", "EnterWorktree",
    "ExitPlanMode", "ExitWorktree", "Glob", "Grep", "ListAgents",
    "ListMcpResourcesTool", "LSP", "Monitor", "NotebookEdit", "PowerShell",
    "PushNotification", "Read", "ReadMcpResourceTool", "RemoteTrigger",
    "ReportFindings", "ScheduleWakeup", "SendFeedback", "SendMessage",
    "SendUserFile", "ShareOnboardingGuide", "Skill", "SubagentHandback",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "TodoWrite", "ToolSearch", "WaitForMcpServers", "WebFetch", "WebSearch",
    "Workflow", "Write",
)


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    (d / "big.txt").write_text("x\n" * 20000, encoding="utf-8")
    return d


def _run(stage: str, payload: dict, flavor: str) -> dict:
    fn = {"pre": hook.main_pre_tool_use, "post": hook.main_post_tool_use,
          "failure": hook.main_post_tool_use_failure}[stage]
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        fn(flavor)
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, out  # exactly one JSON object on every path
    return json.loads(lines[0])


def _denied_or_rewritten(flavor: str, out: dict) -> bool:
    if flavor in ("hermes", "omp", "opencode", "dsh"):
        return out.get("action") in ("block", "rewrite", "ask")
    if flavor == "antigravity":
        return out.get("decision") in ("deny", "ask", "force_ask")
    hso = out.get("hookSpecificOutput", {})
    return hso.get("permissionDecision") in ("deny", "ask") or "updatedInput" in hso


def _edit_rows(ws) -> list[dict]:
    from ctx.edit_outcomes import _rows

    return list(_rows(ws))


def _ledger(ws) -> list[dict]:
    path = ws / ".ctx-session-reads" / "coverage.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ------------------------------------------------------------- defect a
@pytest.mark.parametrize("flavor", ["claude-code", "codex", "hermes", "omp", "opencode", "dsh"])
@pytest.mark.parametrize("name", ["mcp__github__list_issues", "mcp__fs__read_file",
                                  "mcp__x__grep_code", "mcp__x__run_command",
                                  "mcp_github_list_issues", "mcp__plugin_p_db__list"])
def test_mcp_tools_are_never_native_file_or_search_tools(ws, flavor, name):
    payload = {"tool_name": name, "cwd": str(ws), "session_id": "s",
               "tool_input": {"path": str(ws / "big.txt"), "file_path": str(ws / "big.txt"),
                              "command": "grep -rn foo .", "pattern": "foo"}}
    out = _run("pre", payload, flavor)
    assert not _denied_or_rewritten(flavor, out), out
    assert hook._tool_kind(name, flavor) is None
    assert hook._governed_kind(name, flavor) == "mcp"


def test_claude_mcp_server_field_marks_an_mcp_tool():
    payload = {"mcp_server": {"name": "fs", "source": "project"}}
    assert hook._governed_kind("read_file", "codex", payload) == "mcp"
    assert gov.mcp_identity("read_file", payload) == ("fs", "read_file")


def test_opencode_bare_mcp_names_are_gated_not_denied(ws):
    # OpenCode has named MCP tools `<server>_<tool>`: no namespace to detect,
    # so only listed native names get a guard kind.
    out = _run("pre", {"tool_name": "github_list_issues", "cwd": str(ws), "tool_input": {}},
               "opencode")
    assert out.get("action") == "allow"
    assert hook._governed_kind("github_list_issues", "opencode") == "tool"


def test_codex_native_mcp_resource_tools_are_not_listings(ws):
    out = _run("pre", {"tool_name": "list_mcp_resources", "cwd": str(ws), "tool_input": {}},
               "codex")
    assert out == {}


@pytest.mark.parametrize("flavor,name,args", [
    ("codex", "Bash", {"command": "find . -type f"}),
    ("opencode", "bash", {"command": "find . -type f"}),
    ("opencode", "list", {"path": "."}),
    ("antigravity", "view_file", {"AbsolutePath": "BIG"}),
    ("antigravity", "run_command", {"CommandLine": "find . -type f"}),
    ("claude-code", "Grep", {"pattern": "foo"}),
])
def test_native_tools_keep_their_guard(ws, flavor, name, args):
    args = {k: (str(ws / "big.txt") if v == "BIG" else v) for k, v in args.items()}
    out = _run("pre", {"tool_name": name, "cwd": str(ws), "tool_input": args}, flavor)
    assert _denied_or_rewritten(flavor, out), out


# ------------------------------------------------------------- defect b
@pytest.mark.parametrize("name", ["Agent", "Task"])
def test_subagent_results_are_gated_under_both_names(ws, name):
    from ctx.installer import claude_hook_settings

    # The matcher the installer actually writes, so reverting it fails here.
    rx = re.compile(claude_hook_settings("ctx")["hooks"]["PostToolUse"][0]["matcher"])
    assert rx.search(name)
    big = "finding\n" * 4000
    out = _run("post", {"tool_name": name, "cwd": str(ws), "tool_input": {"prompt": "p"},
                        "tool_response": {"content": [{"type": "text", "text": big}],
                                          "totalDurationMs": 3}}, "claude-code")
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    assert uto["totalDurationMs"] == 3  # shape kept: the host validates it
    assert uto["content"][0]["type"] == "text"
    assert "ctx get" in uto["content"][0]["text"]
    assert len(uto["content"][0]["text"]) < len(big) // 4


# ------------------------------------------------------------- defect c
def test_claude_edits_reach_post_and_failure_stages():
    from ctx.installer import claude_hook_settings

    hooks = claude_hook_settings("/x/ctx")["hooks"]
    post = re.compile(hooks["PostToolUse"][0]["matcher"])
    fail = hooks["PostToolUseFailure"][0]
    assert fail["hooks"][0]["command"] == "/x/ctx hook claude-code post-tool-use-failure"
    for name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert post.search(name) and re.search(fail["matcher"], name)


def test_claude_edit_success_is_recorded_and_never_gated(ws):
    original = "line with old_string not found wording\n" * 2000  # must not be scanned
    out = _run("post", {"tool_name": "Edit", "cwd": str(ws), "session_id": "s",
                        "tool_input": {"file_path": str(ws / "a.py"), "old_string": "a",
                                       "new_string": "b"},
                        "tool_response": {"filePath": str(ws / "a.py"), "originalFile": original,
                                          "oldString": "a", "newString": "b"}}, "claude-code")
    assert "updatedToolOutput" not in out.get("hookSpecificOutput", {})
    rows = _edit_rows(ws)
    assert [r["outcome"] for r in rows] == ["applied"]


def test_claude_edit_failure_stage_records_not_found(ws):
    out = _run("failure", {"tool_name": "Edit", "cwd": str(ws), "session_id": "s",
                           "hook_event_name": "PostToolUseFailure",
                           "tool_input": {"file_path": str(ws / "a.py"), "old_string": "zz",
                                          "new_string": "b"},
                           "error": "String to replace not found in file.\nString: zz"},
               "claude-code")
    assert out == {}
    assert [r["outcome"] for r in _edit_rows(ws)] == ["not_found"]
    row = _ledger(ws)[-1]
    assert (row["stage"], row["decision"], row["route"]) == ("post_failure", "failed", "observe")


def test_failure_stage_is_dispatched_by_the_cli(ws):
    payload = {"tool_name": "Bash", "cwd": str(ws), "error": "Exit code 1\nboom"}
    p = subprocess.run([sys.executable, "-m", "ctx", "hook", "claude-code",
                        "post-tool-use-failure"], input=json.dumps(payload), text=True,
                       capture_output=True, timeout=30, cwd=ws)
    assert p.returncode == 0 and json.loads(p.stdout) == {}


# ------------------------------------------------------------- defect d
def test_every_claude_builtin_has_an_explicit_row():
    missing = [t for t in CLAUDE_TOOLS_REFERENCE if t not in gov.NATIVE["claude-code"]]
    assert not missing


@pytest.mark.parametrize("name,kind", [
    ("NotebookRead", "tool"), ("LS", "tool"), ("BashOutput", "tool"), ("TaskOutput", "tool"),
    ("Skill", "skill"), ("ToolSearch", "schema"), ("Agent", "tool"), ("Task", "tool"),
])
def test_previously_undecided_tools_have_explicit_kinds(name, kind):
    assert gov.NATIVE["claude-code"][name] == kind


def test_every_exemption_and_observation_states_a_reason():
    for host, table in gov.NATIVE.items():
        for name, kind in table.items():
            assert kind in gov.KINDS, (host, name)
            pre, post = gov.stage_routes(kind, hook.DIALECT_CAPS[host])
            assert post in gov.ROUTES and (pre is None or pre in gov.ROUTES)
            if "exempt" in (pre, post):
                assert gov.exempt_reason(kind), (host, name)
    for kind in ("edit", "skill"):
        assert gov.OBSERVE_REASONS[kind]


def test_skill_results_are_observed_not_digested(ws):
    body = "step\n" * 6000
    out = _run("post", {"tool_name": "Skill", "cwd": str(ws), "session_id": "s",
                        "tool_input": {"skill": "deploy", "args": "SECRET-ARG"},
                        "tool_response": body}, "claude-code")
    assert out == {}
    row = _ledger(ws)[-1]
    assert row["skill"] == "deploy" and row["route"] == "observe"
    assert "SECRET-ARG" not in json.dumps(_ledger(ws))


# ------------------------------------------------ table <-> hook conformance
@pytest.mark.parametrize("host", gov.HOSTS)
def test_hook_classification_reads_the_table(host, monkeypatch):
    # Not a restatement of the table: inject rows the hook has never heard of
    # and check that classification -- and the guard decision -- follow them.
    probe = "zz_probe_tool"
    assert hook._governed_kind(probe, host) == "tool"
    for kind in ("read", "control", "edit"):
        monkeypatch.setitem(gov.NATIVE[host], probe, kind)
        assert hook._governed_kind(probe, host) == kind
        assert hook._tool_kind(probe, host) == (kind if kind in gov.GUARD_KINDS else None)
    monkeypatch.setitem(gov.NATIVE[host], probe, "command")
    policy = {"_flavor": host, "collapse": True, "steering": "auto"}
    decision = hook.classify({"tool_name": probe, "tool_input": {"command": "find . -type f"}},
                             dict(policy))
    assert decision.get("decision") != "allow" or "rewrite" in decision


@pytest.mark.parametrize("host", gov.HOSTS)
def test_unseen_tool_names_fall_to_the_output_gate(host):
    caps = hook.DIALECT_CAPS[host]
    for name in ("brand_new_tool", "mcp__srv__anything"):
        kind = hook._governed_kind(name, host)
        assert kind in ("tool", "mcp")
        post = gov.stage_routes(kind, caps)[1]
        assert post == ("output_gate" if caps["output_substitution"] else "observe")


def test_dialect_hosts_and_table_hosts_agree():
    assert set(gov.HOSTS) == set(hook.DIALECT_CAPS)
    assert set(gov.NATIVE) == set(gov.HOSTS) == set(gov.LIMITS)


# ------------------------------------------------ registrations <-> table
def test_claude_registrations_are_derived_from_the_table():
    from ctx.installer import claude_hook_settings

    hooks = claude_hook_settings("ctx")["hooks"]
    pre = hooks["PreToolUse"][0]["matcher"]
    post = re.compile(hooks["PostToolUse"][0]["matcher"])
    # Pre: Claude's exact-list form, naming exactly the guard-kind tools.
    assert re.fullmatch(r"[A-Za-z|]+", pre)
    table = gov.NATIVE["claude-code"]
    assert set(pre.split("|")) == {n for n, k in table.items() if k in gov.GUARD_KINDS}
    for name, kind in table.items():
        exempt = gov.stage_routes(kind, hook.DIALECT_CAPS["claude-code"])[1] == "exempt"
        assert bool(post.search(name)) is (not exempt), name
    assert post.search("mcp__any__tool") and post.search("NotInventedYet")


def _plugin(name: str) -> dict:
    return json.loads((ROOT / "plugins" / name / "hooks.json").read_text())


def test_codex_registration_forwards_every_tool():
    hooks = _plugin("codex")["hooks"]
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    assert hooks["PostToolUse"][0]["matcher"] == "*"
    assert "codex" in gov.FORWARDS_ALL


def test_antigravity_registration_matches_the_table():
    agy = _plugin("antigravity")["ctx-harness"]
    pre = set(agy["PreToolUse"][0]["matcher"].split("|"))
    post = set(agy["PostToolUse"][0]["matcher"].split("|"))
    table = gov.NATIVE["antigravity"]
    caps = hook.DIALECT_CAPS["antigravity"]
    for name, kind in table.items():
        pre_route, post_route = gov.stage_routes(kind, caps)
        assert (name in pre) is (pre_route not in (None, "exempt")), name
        wants_post = post_route not in ("exempt",) and kind not in ("edit", "skill")
        assert (name in post) is wants_post, name
    assert "mcp__.*" in post
    assert pre - set(table) == set() and post - set(table) - {"mcp__.*"} == set()
    # Antigravity's documented file tools (hooks reference, supported tools).
    assert {"view_file", "multi_replace_file_content", "list_dir"} <= pre


def test_report_finds_no_ungoverned_native_tool():
    from ctx.coverage import registrations, static_rows, ungoverned_static

    regs = registrations()
    for host in gov.HOSTS:
        loose = ungoverned_static(static_rows(host, regs))
        # Antigravity only sees what its matchers name: an unlisted tool there
        # is the one honest gap, and the report states it.
        assert loose == (["<unlisted tool>"] if host == "antigravity" else []), host


# ------------------------------------------------------- output shapes
@pytest.mark.parametrize("tr,check", [
    ("raw text", lambda v: v == "D"),
    ([{"type": "text", "text": "x"}], lambda v: v == [{"type": "text", "text": "D"}]),
    ({"stdout": "x" * 99, "stderr": "e", "interrupted": False, "isImage": False},
     lambda v: v == {"stdout": "D", "stderr": "", "interrupted": False, "isImage": False}),
    ({"type": "text", "file": {"filePath": "/a", "content": "x" * 999, "numLines": 9}},
     lambda v: v["file"] == {"filePath": "/a", "content": "D", "numLines": 9} and v["type"] == "text"),
    ({"bytes": 9, "code": 200, "result": "page text " * 120, "url": "u"},
     lambda v: v["result"] == "D" and v["code"] == 200),
])
def test_replacement_keeps_the_tool_output_shape(tr, check):
    value, shaped = hook._shape_replacement(tr, "D")
    assert shaped and check(value)


def test_list_of_strings_result_is_shaped_structurally():
    tr = {"mode": "files_with_matches", "numFiles": 5000,
          "filenames": [f"f{i}.py" for i in range(5000)]}
    value, context, shaped = hook._shape_output(tr, "D ctx get run:abcdef123456", 16384)
    assert shaped and value["mode"] == "files_with_matches" and value["numFiles"] == 5000
    assert all(isinstance(f, str) for f in value["filenames"]) and len(value["filenames"]) <= 21
    assert "4980 more items elided" in value["filenames"][-1]
    assert "true lengths: 5000" in context and context.endswith("D ctx get run:abcdef123456")


def test_unshapeable_result_is_flagged_not_guessed():
    tr = {f"k{i}": i for i in range(5000)}  # nothing to shorten: no string, no list
    value, context, shaped = hook._shape_output(tr, "D", 16384)
    assert (value, context, shaped) == (None, None, False)


# ------------------------------------------------------------- ledger
def test_ledger_records_identity_never_arguments(ws):
    _run("pre", {"tool_name": "Bash", "cwd": str(ws), "session_id": "sess-1",
                 "tool_input": {"command": "grep -rn TOPSECRET ."}}, "claude-code")
    _run("post", {"tool_name": "mcp__github__list_commits", "cwd": str(ws), "session_id": "sess-1",
                  "tool_input": {"q": "TOPSECRET"},
                  "tool_response": [{"type": "text", "text": "TOPSECRET " * 4000}]}, "claude-code")
    rows = _ledger(ws)
    assert "TOPSECRET" not in json.dumps(rows)
    pre, post = rows
    assert (pre["stage"], pre["tool"], pre["kind"], pre["route"]) == ("pre", "Bash", "command", "rewrite")
    assert pre["decision"] in ("rewritten", "allowed")
    assert (post["mcp_server"], post["mcp_tool"]) == ("github", "list_commits")
    assert post["route"] == "output_gate" and post["decision"] == "replaced"
    assert post["raw_bytes"] > post["emitted_bytes"] > 0
    assert all(r["schema"] == "ctx.coverage/v1" and r["session"] == "sess-1" for r in rows)


def test_unshaped_replacement_is_recorded_as_such(ws):
    tr = {f"k{i}": i for i in range(5000)}
    out = _run("post", {"tool_name": "LSP", "cwd": str(ws), "tool_input": {},
                        "tool_response": tr}, "claude-code")
    assert "updatedToolOutput" not in out.get("hookSpecificOutput", {})  # nothing ignored is sent
    row = _ledger(ws)[-1]
    assert row["decision"] == "replace_unshaped" and row["emitted_bytes"] == row["raw_bytes"]


def test_filename_flood_is_replaced_in_shape(ws):
    tr = {"mode": "files_with_matches", "numFiles": 5000,
          "filenames": [f"src/f{i}.py" for i in range(5000)]}
    out = _run("post", {"tool_name": "Grep", "cwd": str(ws), "tool_input": {"pattern": "x"},
                        "tool_response": tr}, "claude-code")
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    assert uto["numFiles"] == 5000 and uto["mode"] == "files_with_matches"
    assert len(uto["filenames"]) <= 21 and "more items elided" in uto["filenames"][-1]
    assert "ctx get" in out["hookSpecificOutput"]["additionalContext"]
    assert _ledger(ws)[-1]["decision"] == "replaced"


def test_ledger_failure_never_changes_a_decision(ws):
    (ws / ".ctx-session-reads").write_text("not a directory")
    out = _run("pre", {"tool_name": "Bash", "cwd": str(ws),
                       "tool_input": {"command": "find . -type f"}}, "claude-code")
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "updatedInput" in out["hookSpecificOutput"]
    out = _run("post", {"tool_name": "Bash", "cwd": str(ws),
                        "tool_response": {"stdout": "y" * 40000, "stderr": ""}}, "claude-code")
    assert "ctx" in out["hookSpecificOutput"]["updatedToolOutput"]["stdout"]


# ------------------------------------------------------------- report
def test_coverage_report_static_and_observed(ws):
    _run("post", {"tool_name": "WebFetch", "cwd": str(ws), "session_id": "a",
                  "tool_response": {"bytes": 1, "code": 200, "result": "z" * 40000, "url": "u"}},
         "claude-code")
    _run("failure", {"tool_name": "Bash", "cwd": str(ws), "session_id": "b",
                     "error": "Exit code 1\n" + "boom\n" * 300}, "claude-code")
    p = subprocess.run([sys.executable, "-m", "ctx", "coverage", "--json"], cwd=ws,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["schema"] == "ctx.coverage-report/v1"
    assert set(data["hosts"]) == set(gov.HOSTS)
    agy = data["hosts"]["antigravity"]
    assert agy["caps"] == {"input_substitution": False, "output_substitution": False}
    assert any("@-referenced" in x for x in data["hosts"]["claude-code"]["cannot_govern"])
    obs = data["observed"]
    assert obs["sessions"] == 2
    top = obs["ungoverned"][0]
    assert top["tool"] == "claude-code Bash" and top["why"] == {"failed": 1}
    last = subprocess.run([sys.executable, "-m", "ctx", "coverage", "--host", "claude-code",
                           "--session", "b"], cwd=ws, capture_output=True, text=True, timeout=60)
    assert last.returncode == 0, last.stderr
    assert "claude-code  (rewrite input: yes, replace output: yes)" in last.stdout
    assert "codex  (" not in last.stdout
    assert "WebFetch" not in last.stdout.split("\nobserved  (")[1]  # filtered to session b


def test_report_states_that_strict_has_no_code_path(ws):
    (ws / "ctx.toml").write_text('version = 1\n[guard]\nmode = "strict"\n', encoding="utf-8")
    p = subprocess.run([sys.executable, "-m", "ctx", "coverage", "--host", "codex"], cwd=ws,
                       capture_output=True, text=True, timeout=60)
    assert 'mode = "strict" has no code path of its own' in p.stdout


def test_report_flags_a_stale_installed_matcher(ws):
    settings = {"hooks": {"PostToolUse": [{"matcher": "Bash|Read|Task|mcp__.*", "hooks": [
        {"type": "command", "command": "/usr/bin/ctx hook claude-code post-tool-use"}]}]}}
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text(json.dumps(settings))
    from ctx.coverage import installed_drift

    notes = installed_drift(ws)
    assert any("PostToolUse matcher 'Bash|Read|Task|mcp__.*' predates" in n for n in notes)
    assert any("no ctx PreToolUse hook" in n for n in notes)


# ------------------------------------------------------------- defect f
# Live Antigravity CLI (agy 1.5.5) nests the call under `toolCall`. These
# fixtures keep the captured payloads' exact key set and nesting; paths, ids
# and the prompt are replaced with placeholders.
def _agy_payload(ws, event: str, name: str, args: dict, **extra) -> dict:
    payload = {
        "artifactDirectoryPath": str(ws / ".agy" / "brain" / "conv-0000"),
        "conversationId": "conv-0000",
        "cwd": str(ws),
        "hookEventName": event,
        "lastUserInput": "placeholder prompt",
        "modelName": "gemini-3.6-flash-medium",
        "stepIdx": 3,
        "toolCall": {"args": {**args, "toolSummary": "placeholder"},
                     "id": "call_0000", "name": name},
        "transcriptPath": str(ws / ".agy" / "transcript_full.jsonl"),
        "workspacePaths": [str(ws)],
    }
    payload.update(extra)
    return payload


def test_antigravity_nested_tool_call_is_guarded(ws):
    (ws / "huge.log").write_text("log line\n" * 20000, encoding="utf-8")
    payload = _agy_payload(ws, "PreToolUse", "run_command",
                           {"CommandLine": "cat huge.log", "WaitMsBeforeAsync": 5000})
    out = _run("pre", payload, "antigravity")
    assert out["decision"] == "deny", out  # was {"decision": "allow"}
    assert "ctx" in out["reason"]
    row = _ledger(ws)[-1]
    assert (row["tool"], row["kind"], row["session"]) == ("run_command", "command", "conv-0000")


def test_antigravity_nested_view_file_is_bounded(ws):
    payload = _agy_payload(ws, "PreToolUse", "view_file", {"AbsolutePath": str(ws / "big.txt")})
    assert _run("pre", payload, "antigravity")["decision"] == "deny"


def test_antigravity_nested_post_is_recorded_without_a_result(ws):
    payload = _agy_payload(ws, "PostToolUse", "run_command", {"CommandLine": "echo hi"}, error="")
    assert _run("post", payload, "antigravity") == {}
    row = _ledger(ws)[-1]
    assert (row["stage"], row["tool"], row["decision"]) == ("post", "run_command", "no_result")


def test_normalizer_keeps_flat_fields_and_other_shapes():
    flat = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert hook._normalize_payload(flat) == flat
    camel = {"toolName": "Read", "toolCall": {"name": "X", "args": {}}}
    assert hook._normalize_payload(camel)["toolName"] == "Read"
    assert "tool_name" not in hook._normalize_payload(camel)
    assert hook._normalize_payload([1, 2]) == {}
    nested = hook._normalize_payload({"toolCall": {"name": "run_command", "args": {"a": 1},
                                                    "id": "i", "result": "r"}})
    assert (nested["tool_name"], nested["tool_input"], nested["tool_use_id"],
            nested["tool_response"]) == ("run_command", {"a": 1}, "i", "r")


# ------------------------------------------- review: live shapes and media
# Result shapes observed from Claude Code 2.1.322 (live PostToolUse payloads).
LIVE_BASH = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
             "noOutputExpected": False}
LIVE_READ_TEXT = {"type": "text", "file": {"filePath": "/w/a.txt", "content": "",
                                            "numLines": 2, "startLine": 1, "totalLines": 2}}


def test_live_bash_shape_is_kept(ws):
    tr = dict(LIVE_BASH, stdout="line\n" * 9000)
    out = _run("post", {"tool_name": "Bash", "cwd": str(ws), "tool_input": {"command": "x"},
                        "tool_response": tr}, "claude-code")
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    assert set(uto) == set(LIVE_BASH)
    assert {k: uto[k] for k in uto if k != "stdout"} == {k: v for k, v in LIVE_BASH.items() if k != "stdout"}
    assert uto["stdout"].startswith("[ctx run:")


def test_live_read_shape_is_kept(ws):
    tr = {"type": "text", "file": dict(LIVE_READ_TEXT["file"], content="row\n" * 9000,
                                       numLines=9000, totalLines=9000)}
    out = _run("post", {"tool_name": "Read", "cwd": str(ws), "tool_input": {"file_path": "/w/a.txt"},
                        "tool_response": tr}, "claude-code")
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    assert uto["type"] == "text" and set(uto["file"]) == set(LIVE_READ_TEXT["file"])
    assert uto["file"]["numLines"] == 9000 and uto["file"]["content"].startswith("[ctx run:")


@pytest.mark.parametrize("tool,tr", [
    ("Read", {"type": "image", "file": {"base64": "iVBOR" + "A" * 60000, "type": "image/png",
                                        "originalSize": 45000}}),
    ("Read", {"type": "pdf", "file": {"filePath": "/w/a.pdf", "base64": "JVBER" + "A" * 60000}}),
    ("Bash", dict(LIVE_BASH, stdout="data:image/png;base64," + "A" * 60000, isImage=True)),
    ("Agent", {"content": [{"type": "text", "text": "x" * 30000},
                           {"type": "image", "source": {"type": "base64", "data": "A" * 30000}}]}),
    ("Read", {"type": "notebook", "file": {"cells": [{"outputs": [{"data": {
        "image/png": "iVBOR" + "A" * 60000, "text/plain": "<Figure>"}}]}]}}),
    ("WebFetch", {"result": "data:image/png;base64," + "A" * 60000, "code": 200}),
])
def test_media_results_are_never_substituted(ws, tool, tr):
    out = _run("post", {"tool_name": tool, "cwd": str(ws), "tool_input": {},
                        "tool_response": tr}, "claude-code")
    assert "updatedToolOutput" not in out.get("hookSpecificOutput", {})
    row = _ledger(ws)[-1]
    assert row["decision"] == "media_untouched" and row["note"].startswith("media:")


def test_gate_failure_digest_is_shaped_for_builtins(ws, monkeypatch):
    import ctx.digest as digest_mod

    def boom(*a, **k):
        raise RuntimeError("digest broke")

    monkeypatch.setattr(digest_mod, "digest_output", boom)
    tr = dict(LIVE_BASH, stdout="y\n" * 20000)
    out = _run("post", {"tool_name": "Bash", "cwd": str(ws), "tool_input": {"command": "x"},
                        "tool_response": tr}, "claude-code")
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(uto, dict) and set(uto) == set(LIVE_BASH)
    assert "gate-failed" in uto["stdout"]
    tr = {"mode": "files_with_matches", "filenames": [f"f{i}.py" for i in range(5000)]}
    out = _run("post", {"tool_name": "Glob", "cwd": str(ws), "tool_input": {},
                        "tool_response": tr}, "claude-code")
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    assert uto["mode"] == "files_with_matches" and len(uto["filenames"]) <= 21
    assert "gate-failed" in out["hookSpecificOutput"]["additionalContext"]


# ------------------------------------------- review: edits only by table
@pytest.mark.parametrize("host,name", [
    ("codex", "TodoWrite"), ("antigravity", "write_stdin"), ("hermes", "str_replace_editor"),
    ("omp", "file_editor"), ("dsh", "WriteFile"),
])
def test_unlisted_edit_like_names_are_gated(ws, host, name):
    assert hook._governed_kind(name, host) == "tool"
    if host == "antigravity":
        return  # cannot replace output; the kind is what matters
    out = _run("post", {"tool_name": name, "cwd": str(ws), "tool_input": {},
                        "tool_response": "file body\n" * 5000}, host)
    assert out, out  # replaced (native: {"output": ...}; codex: decision block)
    assert _ledger(ws)[-1]["decision"] == "replaced"


# ------------------------------------------- review: installer migration
def test_reinstall_updates_owned_matchers_and_leaves_foreign_ones(tmp_path):
    from ctx.installer import claude_hook_settings, install_claude
    from ctx.workspace import resolve_workspace

    (tmp_path / ".claude").mkdir()
    old = {"hooks": {
        "PreToolUse": [{"matcher": "Bash|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit",
                        "hooks": [{"type": "command", "command": "/old/ctx hook claude-code pre-tool-use"}]}],
        "PostToolUse": [
            {"matcher": "Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*",
             "hooks": [{"type": "command", "command": "/old/ctx hook claude-code post-tool-use",
                        "timeout": 10}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": "prettier --write"}]},
            {"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/old/ctx hook claude-code post-tool-use"},
                {"type": "command", "command": "audit-tool"}]},
        ],
    }, "statusLine": {"type": "command", "command": "mine"}}
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(old))
    report = install_claude(resolve_workspace(str(tmp_path)), init_policy=False)
    new = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    want = claude_hook_settings("ctx")["hooks"]
    post = new["hooks"]["PostToolUse"]
    assert post[0]["matcher"] == want["PostToolUse"][0]["matcher"]
    assert post[0]["hooks"][0]["command"] == "/old/ctx hook claude-code post-tool-use"
    assert post[1] == old["hooks"]["PostToolUse"][1]  # foreign: untouched
    assert post[2] == old["hooks"]["PostToolUse"][2]  # mixed: untouched
    assert new["hooks"]["PreToolUse"] == old["hooks"]["PreToolUse"]  # already current
    assert "PostToolUseFailure" in new["hooks"]
    assert ("updated ctx PostToolUse matcher from a previous default "
            "(was 'Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*')") in report
    # The mixed group cannot be changed, and setup says so.
    assert "ctx PostToolUse hook shares a group with another hook (matcher 'Bash')" in report
    again = install_claude(resolve_workspace(str(tmp_path)), init_policy=False)
    assert "updated ctx" not in again


def _settings_after_install(tmp_path, settings: dict) -> tuple[dict, str]:
    from ctx.installer import install_claude
    from ctx.workspace import resolve_workspace

    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(settings))
    report = install_claude(resolve_workspace(str(tmp_path)), init_policy=False)
    return json.loads((tmp_path / ".claude" / "settings.json").read_text()), report


@pytest.mark.parametrize("command", [
    "echo done; /opt/other/ctx hook claude-code post-tool-use",
    "/home/u/bin/mylogger --forward-to 'ctx hook claude-code post-tool-use'",
    "/opt/ctx hook claude-code post-tool-use > /tmp/log",
    "/opt/ctx hook claude-code post-tool-use && notify",
    "time /opt/ctx hook claude-code post-tool-use",
    "/opt/notctx hook claude-code post-tool-use",
])
def test_user_commands_ending_like_ctx_are_not_owned(tmp_path, command):
    from ctx.installer import _is_ctx_hook_command, claude_hook_settings

    assert not _is_ctx_hook_command(command, "post-tool-use")
    user = {"matcher": "Write", "hooks": [{"type": "command", "command": command}]}
    new, _ = _settings_after_install(tmp_path, {"hooks": {"PostToolUse": [user]}})
    post = new["hooks"]["PostToolUse"]
    assert post[0] == user  # untouched
    want = claude_hook_settings("ctx")["hooks"]["PostToolUse"][0]["matcher"]
    assert any(g.get("matcher") == want and g["hooks"][0]["command"].endswith(
        "hook claude-code post-tool-use") for g in post[1:])  # the real ctx hook was added


@pytest.mark.parametrize("command", [
    "/old/venv/bin/ctx hook claude-code post-tool-use",
    "'/path with space/ctx' hook claude-code post-tool-use",
    "/usr/bin/python3.13 -m ctx hook claude-code post-tool-use",
])
def test_installer_written_commands_are_owned(command):
    from ctx.installer import _is_ctx_hook_command

    assert _is_ctx_hook_command(command, "post-tool-use")
    assert not _is_ctx_hook_command(command, "pre-tool-use")


def test_earlier_native_shim_form_is_ctx_owned():
    from ctx.installer import _is_ctx_hook_command

    # wrap.py once wrote the Rust shim for this stage (git history: _post_hook_exe).
    assert _is_ctx_hook_command("/usr/local/bin/ctx-hook-native hook claude-code post-tool-use",
                                "post-tool-use")
    assert not _is_ctx_hook_command("uvx ctx-harness hook claude-code post-tool-use", "post-tool-use")


def test_duplicate_ctx_groups_are_reported(tmp_path):
    def group(matcher, cmd):
        return {"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]}

    old_default = "Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*"
    settings = {"hooks": {"PostToolUse": [
        group(old_default, "/old/venv/bin/ctx-hook-native hook claude-code post-tool-use"),
        group(old_default, "/usr/bin/ctx hook claude-code post-tool-use"),
    ]}}
    new, report = _settings_after_install(tmp_path, settings)
    assert "ctx PostToolUse hook is registered 2 times" in report
    assert "Keep one: delete the extra ctx `hook claude-code post-tool-use` entries" in report
    assert len(new["hooks"]["PostToolUse"]) == 2  # nothing deleted on the user's behalf


def test_customized_ctx_matcher_is_kept_and_reported(tmp_path):
    from ctx.installer import claude_hook_settings

    narrowed = {"matcher": "Bash|Read", "hooks": [
        {"type": "command", "command": "/x/ctx hook claude-code post-tool-use"}]}
    new, report = _settings_after_install(tmp_path, {"hooks": {"PostToolUse": [narrowed]}})
    assert new["hooks"]["PostToolUse"] == [narrowed]
    want = claude_hook_settings("ctx")["hooks"]["PostToolUse"][0]["matcher"]
    assert f"ctx PostToolUse matcher is customized ('Bash|Read'); left as is. Current default: {want!r}" in report
    # PreToolUse "Bash|Read" WAS a shipped default, so it is upgraded.
    old_pre = {"matcher": "Bash|Read", "hooks": [
        {"type": "command", "command": "/x/ctx hook claude-code pre-tool-use"}]}
    new, report = _settings_after_install(tmp_path, {"hooks": {"PreToolUse": [old_pre]}})
    assert new["hooks"]["PreToolUse"][0]["matcher"] == claude_hook_settings("ctx")["hooks"]["PreToolUse"][0]["matcher"]


# ------------------------------------------- review: bounded ledger
def test_coverage_ledger_rotates_and_report_reads_two_generations(ws, monkeypatch):
    from ctx import coverage

    monkeypatch.setattr(hook, "COVERAGE_MAX_BYTES", 2000)
    for i in range(60):
        hook._note_coverage(str(ws), "codex", "post", {"tool_name": f"t{i}"}, "tool",
                            "output_gate", "pass", 1, 1)
    d = ws / ".ctx-session-reads"
    assert (d / "coverage.1.jsonl").exists()
    assert (d / "coverage.jsonl").stat().st_size <= 2000 + 400
    assert (d / "coverage.1.jsonl").stat().st_size <= 2000 + 400
    rows = coverage.read_ledger(ws)
    assert 0 < len(rows) < 60 and rows[-1]["tool"] == "t59"


def test_report_read_is_capped_whatever_the_file_size(ws, monkeypatch):
    from ctx import coverage

    monkeypatch.setattr(coverage, "_READ_CAP_BYTES", 4096)
    d = ws / ".ctx-session-reads"
    d.mkdir(exist_ok=True)
    row = json.dumps({"schema": "ctx.coverage/v1", "tool": "x", "host": "codex"})
    (d / "coverage.jsonl").write_text((row + "\n") * 5000)
    rows = coverage.read_ledger(ws)
    assert 0 < len(rows) <= 4096 // len(row) + 1


# ------------------------------------------- review: accurate agy deny text
def test_antigravity_large_read_deny_does_not_claim_a_bound(ws):
    payload = _agy_payload(ws, "PreToolUse", "view_file", {"AbsolutePath": str(ws / "big.txt")})
    out = _run("pre", payload, "antigravity")
    assert out["decision"] == "deny"
    assert "bounded to the first" not in out["reason"]
    assert "ctx get repo:" in out["reason"]


# ------------------------------------------- review 2: agy view_file loop
def test_antigravity_view_file_advice_followed_verbatim_succeeds(ws):
    path = str(ws / "big.txt")
    first = _run("pre", _agy_payload(ws, "PreToolUse", "view_file", {"AbsolutePath": path}),
                 "antigravity")
    assert first["decision"] == "deny"
    m = re.search(r"re-issue the call with ((?:\w+=\d+)(?: and \w+=\d+)*)", first["reason"])
    assert m, first["reason"]
    advised = {k: int(v) for k, v in (kv.split("=") for kv in m.group(1).split(" and "))}
    # Only parameters view_file has (Antigravity hooks reference).
    assert set(advised) <= {"AbsolutePath", "StartLine", "EndLine", "IsSkillFile"}
    again = _run("pre", _agy_payload(ws, "PreToolUse", "view_file",
                                     {"AbsolutePath": path, **advised}), "antigravity")
    assert again["decision"] == "allow", again


@pytest.mark.parametrize("start,end,allowed", [(1, 240, True), (500, 700, True), (1, 5000, False)])
def test_antigravity_ranged_view_file_is_a_bounded_read(ws, start, end, allowed):
    out = _run("pre", _agy_payload(ws, "PreToolUse", "view_file",
                                   {"AbsolutePath": str(ws / "big.txt"), "StartLine": start,
                                    "EndLine": end}), "antigravity")
    assert (out["decision"] == "allow") is allowed, out


def test_claude_read_bound_still_uses_limit(ws):
    out = _run("pre", {"tool_name": "Read", "cwd": str(ws),
                       "tool_input": {"file_path": str(ws / "big.txt")}}, "claude-code")
    assert out["hookSpecificOutput"]["updatedInput"]["limit"] == 240
    assert "StartLine" not in out["hookSpecificOutput"]["updatedInput"]


# ------------------------------------------- review 2: concurrent rotation
def _rot_worker(ws: str, i: int, n: int, cap: int) -> None:
    from ctx import hook as h

    h.COVERAGE_MAX_BYTES = cap
    for j in range(n):
        h._note_coverage(ws, "claude-code", "post", {"tool_name": "Bash", "session_id": f"w{i}"},
                         "command", "output_gate", "pass", j, j)


def test_concurrent_rotation_never_clobbers_the_kept_generation(tmp_path):
    import multiprocessing as mp

    cap = 16 * 1024
    ctx_mp = mp.get_context("fork")
    procs = [ctx_mp.Process(target=_rot_worker, args=(str(tmp_path), i, 600, cap)) for i in range(8)]
    for p in procs:
        p.start()
    kept = tmp_path / ".ctx-session-reads" / "coverage.1.jsonl"
    sizes = []
    while any(p.is_alive() for p in procs):
        try:
            sizes.append(kept.stat().st_size)
        except OSError:
            pass
    for p in procs:
        p.join()
        assert p.exitcode == 0
    assert sizes, "rotation never happened"
    assert min(sizes) > cap, f"a second rotation replaced the kept generation ({min(sizes)} <= {cap})"
    for name in ("coverage.1.jsonl", "coverage.jsonl"):
        for line in (tmp_path / ".ctx-session-reads" / name).read_text().splitlines():
            json.loads(line)  # no torn lines



# ------------------------------------------- review 2: the gate's post-condition
def _same_shape(orig, new, exact: bool = True) -> bool:
    """``new`` is valid in ``orig``'s shape: same types, same keys, list
    elements of kinds the original list held. Scalars outside lists must be
    unchanged (counts are never edited); inside a list, the type suffices."""
    if isinstance(orig, dict):
        return isinstance(new, dict) and set(new) == set(orig) and all(
            _same_shape(orig[k], new[k], exact) for k in orig)
    if isinstance(orig, list):
        if not isinstance(new, list):
            return False
        if not orig:
            return new == []
        return all(any(_same_shape(o, n, False) for o in orig[:3]) for n in new)
    if isinstance(orig, str):
        return isinstance(new, str)
    return type(new) is type(orig) and (new == orig or not exact)


def _gen_value(rng, depth=0):
    import random as _r  # noqa: F401  (rng is a random.Random)
    roll = rng.random()
    if depth >= 3 or roll < 0.35:
        n = rng.choice([0, 5, 80, 2000, 30000, 70000])
        style = rng.choice(["text", "path", "url", "words"])
        if style == "path":
            return "/" + "/".join(f"d{i}" for i in range(max(1, n // 3)))[:max(1, n)]
        if style == "url":
            return ("https://example.test/" + "a" * n)[:max(1, n + 20)]
        if style == "words":
            return "\n".join(f"w{i}" for i in range(max(1, n // 4)))
        return ("lorem ipsum dolor sit amet " * (n // 27 + 1))[:n]
    if roll < 0.45:
        return rng.choice([0, 1, 12345, True, False, None, 3.5])
    if roll < 0.7:
        kind = rng.choice(["str", "obj", "blocks"])
        n = rng.choice([0, 3, 50, 3000])
        if kind == "str":
            return [f"src/f{i}.py" for i in range(n)]
        if kind == "blocks":
            return [{"type": "text", "text": "x" * rng.choice([10, 9000])} for _ in range(max(1, n // 50))]
        return [{"path": f"a/{i}", "size": i, "body": "y" * rng.choice([3, 400])} for i in range(n)]
    return {f"f{i}": _gen_value(rng, depth + 1) for i in range(rng.randint(1, 4))}


def test_gate_post_condition_over_generated_shapes(ws):
    import base64
    import random

    rng = random.Random(20261111)
    budget = 16384
    media = base64.b64encode(bytes(range(256)) * 300).decode()
    checked = replaced = 0
    for i in range(160):
        tr = {"f0": _gen_value(rng, 1), "f1": _gen_value(rng, 1), "f2": _gen_value(rng, 2)}
        is_media = i % 9 == 0
        if is_media:
            tr["thumb"] = {"type": "image", "source": {"type": "base64", "data": media}}
        raw_size = len(json.dumps(tr, ensure_ascii=False).encode())
        out = _run("post", {"tool_name": "LSP", "cwd": str(ws), "tool_input": {},
                            "session_id": f"g{i}", "tool_response": tr}, "claude-code")
        hso = out.get("hookSpecificOutput", {})
        row = _ledger(ws)[-1]
        if is_media:
            assert "updatedToolOutput" not in hso and row["decision"] == "media_untouched"
            continue
        if "updatedToolOutput" in hso:
            uto = hso["updatedToolOutput"]
            assert _same_shape(tr, uto), (i, json.dumps(uto)[:300])
            ctx_text = hso.get("additionalContext", "")
            sent = len(json.dumps(uto, ensure_ascii=False).encode()) + len(ctx_text.encode())
            assert sent <= budget, (i, sent)
            assert row["decision"] in ("replaced", "gate_failed")
            assert row["emitted_bytes"] == sent  # the ledger measures what was sent
            replaced += 1
        else:
            assert row["decision"] in ("pass", "replace_unshaped"), row
            assert row["emitted_bytes"] == row["raw_bytes"]
            if row["decision"] == "pass":
                assert raw_size <= budget * 1.2  # gate measures normalized text; json close
        checked += 1
    assert replaced >= 20 and checked >= 100


@pytest.mark.parametrize("label,tr,field", [
    ("result+commentary", {"result": "r " * 55000, "commentary": "c " * 45000}, "result"),
    ("long path beside text", {"filePath": "/x/" + "d/" * 20000, "result": "short result " * 300}, None),
])
def test_digest_never_lands_in_the_wrong_field(ws, label, tr, field):
    out = _run("post", {"tool_name": "LSP", "cwd": str(ws), "tool_input": {},
                        "tool_response": tr}, "claude-code")
    hso = out["hookSpecificOutput"]
    uto = hso["updatedToolOutput"]
    sent = len(json.dumps(uto, ensure_ascii=False).encode()) + len(hso.get("additionalContext", "").encode())
    assert sent <= 16384 and _ledger(ws)[-1]["emitted_bytes"] == sent
    if field:
        assert uto[field].startswith("[ctx run:")
    assert not uto.get("filePath", "").startswith("[ctx run:")  # never over a path



# ------------------------------------------- review 3: text is never "media"
def _misjudged_samples() -> dict[str, str]:
    """The reviewer's list of text the content heuristic took for media."""
    import base64 as b64mod
    import hashlib
    import random

    rnd = random.Random(1)

    def rb(n):
        return bytes(rnd.getrandbits(8) for _ in range(n))

    mime = b64mod.encodebytes(rb(30000)).decode()
    samples = {
        "hex dump (xxd -p)": "\n".join(rb(30).hex() for _ in range(2000)),
        "hex single line": rb(20000).hex(),
        "single-letter run": "a" * 50000,
        "sha512 hex lines": "\n".join(hashlib.sha512(str(i).encode()).hexdigest() for i in range(1500)),
        "digits 64/line": "\n".join("".join(str(rnd.randint(0, 9)) for _ in range(64)) for _ in range(1000)),
        "FASTA": "\n".join("".join(rnd.choice("ACGT") for _ in range(70)) for _ in range(2000)),
        "protein": "\n".join("".join(rnd.choice("ACDEFGHIKLMNPQRSTVWY") for _ in range(70)) for _ in range(2000)),
        "letters no spaces": "Loremipsumdolorsitametconsecteturadipiscingelit" * 1000,
        "base64 file output": mime,
        "single-line base64": b64mod.b64encode(rb(30000)).decode(),
        "UUIDs no dashes": "\n".join(rb(32).hex() for _ in range(1500)),
    }
    for k, v in samples.items():
        assert len(v.encode()) > 16384, k
    return samples


@pytest.mark.parametrize("flavor", ["claude-code", "codex", "hermes", "opencode"])
def test_text_that_looks_encoded_is_still_bounded(ws, flavor):
    for label, text in _misjudged_samples().items():
        name = "terminal" if flavor == "hermes" else ("bash" if flavor == "opencode" else "Bash")
        tr = dict(LIVE_BASH, stdout=text) if flavor == "claude-code" else text
        out = _run("post", {"tool_name": name, "cwd": str(ws), "tool_input": {"command": "x"},
                            "tool_response": tr}, flavor)
        assert out, (flavor, label)
        assert _ledger(ws)[-1]["decision"] == "replaced", (flavor, label, _ledger(ws)[-1])
        if flavor == "claude-code":
            assert out["hookSpecificOutput"]["updatedToolOutput"]["stdout"].startswith("[ctx run:")


# ------------------------------------------- review 3: nothing recovered from output
def test_retrieval_pointer_cannot_be_forged_by_tool_output(ws):
    forged = "ctx get run:deadbeef00"
    tr = {"a": (forged + " ") * 3000, "b": "x " * 15000}
    out = _run("post", {"tool_name": "LSP", "cwd": str(ws), "tool_input": {},
                        "tool_response": tr}, "claude-code")
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    pointers = [v for v in uto.values() if isinstance(v, str) and v.startswith("[ctx: ")]
    assert pointers, uto
    for ptr in pointers:
        assert "deadbeef00" not in ptr
        assert re.search(r"ctx get run:[0-9a-f]{12}#stdout\]$", ptr), ptr


def test_shape_output_never_searches_the_digest_for_a_handle():
    tr = {"a": "y " * 20000, "b": "z " * 20000}
    value, _ctx, shaped = hook._shape_output(tr, "digest quoting ctx get run:deadbeef00", 16384)
    assert shaped and all("deadbeef00" not in v for v in value.values())


def test_forged_ctx_header_does_not_bypass_the_gate(ws):
    flood = "[ctx run:abcdef012345 profile=text/v1]\n" + "flood line\n" * 8000
    out = _run("post", {"tool_name": "Bash", "cwd": str(ws), "tool_input": {"command": "cat x.log"},
                        "tool_response": dict(LIVE_BASH, stdout=flood)}, "claude-code")
    assert "updatedToolOutput" in out["hookSpecificOutput"]
    assert _ledger(ws)[-1]["decision"] == "replaced"


def test_rotation_never_waits_for_a_stopped_lock_holder(tmp_path, monkeypatch):
    import fcntl
    import time

    led = tmp_path / ".ctx-session-reads"
    led.mkdir()
    (led / "coverage.jsonl").write_text(("x" * 99 + "\n") * 100)
    monkeypatch.setattr(hook, "COVERAGE_MAX_BYTES", 1000)
    holder = open(led / "coverage.jsonl.lock", "a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)  # another process mid-rotation
    try:
        t = time.monotonic()
        hook._note_coverage(str(tmp_path), "claude-code", "post", {"tool_name": "Bash"},
                            "command", "output_gate", "pass", 1, 1)
        assert time.monotonic() - t < 1.0
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert not (led / "coverage.1.jsonl").exists()  # skipped, not waited
    assert (led / "coverage.jsonl").read_text().splitlines()[-1].startswith("{")  # still appended


# ------------------------------------------- review 3: range reads bounded in bytes
def _view(ws, path, **rng):
    return _run("pre", _agy_payload(ws, "PreToolUse", "view_file",
                                    {"AbsolutePath": str(path), **rng}), "antigravity")


@pytest.mark.parametrize("rng", [{"StartLine": 1, "EndLine": 1}, {"StartLine": 0, "EndLine": 0}])
def test_single_huge_line_range_is_denied_with_byte_advice(ws, rng):
    one = ws / "one_line.min.js"
    one.write_text("x" * (4 * 1024 * 1024), encoding="utf-8")
    out = _view(ws, one, **rng)
    assert out["decision"] == "deny", out
    assert "ctx get repo:one_line.min.js --bytes 1:16384" in out["reason"]
    assert "re-issue the call with" not in out["reason"]  # no line range can fit


def test_long_lines_range_advice_followed_verbatim_succeeds(ws):
    wide = ws / "wide.log"
    wide.write_text(("w" * 999 + "\n") * 500, encoding="utf-8")  # 1000-byte lines
    first = _view(ws, wide, StartLine=1, EndLine=100)  # 100 lines but ~100 KB
    assert first["decision"] == "deny"
    m = re.search(r"re-issue the call with StartLine=(\d+) and EndLine=(\d+)", first["reason"])
    assert m, first["reason"]
    again = _view(ws, wide, StartLine=int(m[1]), EndLine=int(m[2]))
    assert again["decision"] == "allow", again
    whole = _view(ws, wide)  # whole-file read: advice must also fit in bytes
    m = re.search(r"re-issue the call with StartLine=(\d+) and EndLine=(\d+)", whole["reason"])
    assert m and _view(ws, wide, StartLine=int(m[1]), EndLine=int(m[2]))["decision"] == "allow"


def test_zero_zero_range_is_the_first_line(ws):
    small = ws / "small_first.txt"
    small.write_text("short first line\n" + "z" * 40000, encoding="utf-8")
    assert _view(ws, small, StartLine=0, EndLine=0)["decision"] == "allow"


def test_range_scan_is_bounded():
    import os as _os

    assert hook._RANGE_SCAN_CAP <= 8 * 1024 * 1024
    assert hook._range_scan(_os.devnull, 0, 10, 100) == (0, 0)


# ------------------------------------------- review 3: nudges count against the budget
@pytest.mark.parametrize("nudge_bytes", [3000, 15500, 20000])
def test_nudge_text_is_inside_the_budget(ws, monkeypatch, nudge_bytes):
    monkeypatch.setattr(hook, "_emission_nudge", lambda payload: "N" * nudge_bytes)
    tr = {"a": "y " * 20000, "b": [f"f{i}" for i in range(3000)]}
    out = _run("post", {"tool_name": "LSP", "cwd": str(ws), "tool_input": {},
                        "tool_response": tr}, "claude-code")
    hso = out["hookSpecificOutput"]
    row = _ledger(ws)[-1]
    if "updatedToolOutput" in hso:
        sent = (len(json.dumps(hso["updatedToolOutput"], ensure_ascii=False).encode())
                + len(hso["additionalContext"].encode()))
        assert sent <= 16384 and row["emitted_bytes"] == sent
    else:
        assert row["decision"] == "replace_unshaped" and nudge_bytes > 15000
