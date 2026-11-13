"""Replay real Antigravity CLI hook payloads through the real hook entry point.

The fixtures under ``tests/fixtures/antigravity/agy-1.5.5/`` are payloads
captured live from ``agy`` 1.5.5 (Gemini 3.6 Flash, headless), sanitized:
the workspace is ``/workspace/project``, the home ``/home/user``, the
conversation id and tool-call id are zeroed. Structure and key sets are
exactly what the host sent. The older tests that build flat
``tool_name``/``tool_input`` payloads by hand stay as compatibility cases
for a host that predates this shape; these are the real contract.

Observed on 1.5.5 and pinned here:

* PreToolUse answers ``{"decision": ..., "overwrite": {...}}``; the host
  merges ``overwrite`` into ``toolCall.args`` before the tool runs (live:
  run_command ``CommandLine``, read_file ``StartLine``/``EndLine``). A bare
  ``{}`` is read as a deny, so every path names a decision.
* PostToolUse accepts only ``{}`` and carries no tool result.
* SessionStart fires once; a ``systemMessage`` step injected there persists.
  PreInvocation fires before every model call; its steps are ephemeral.
"""

from __future__ import annotations

import io
import json
import shlex
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ctx import hook

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "antigravity" / "agy-1.5.5"

#: fixture file -> (hookEventName, ctx hook stage the plugin registers, or None)
FIXTURE_EVENTS = {
    "session_start.json": ("SessionStart", "session-start"),
    "pre_invocation.json": ("PreInvocation", "pre-invocation"),
    "pre_tool_use_run_command.json": ("PreToolUse", "pre-tool-use"),
    "pre_tool_use_read_file.json": ("PreToolUse", "pre-tool-use"),
    "post_tool_use_run_command.json": ("PostToolUse", "post-tool-use"),
    "post_tool_use_read_file.json": ("PostToolUse", "post-tool-use"),
    "post_invocation.json": ("PostInvocation", None),
    "stop.json": ("Stop", None),
}

#: The top-level payload keys the hook reads, per event. Measured, not
#: assumed: ``test_hook_reads_exactly_the_declared_keys`` replays every
#: fixture with an instrumented payload and compares. ``toolCall`` is read by
#: the boundary normalizer, which lifts ``name``/``args``/``id`` to the flat
#: names the rest of the hook uses.
HOOK_READS = {
    "PreToolUse": {"toolCall", "conversationId", "cwd", "hookEventName", "workspacePaths"},
    "PostToolUse": {"toolCall", "conversationId", "cwd", "workspacePaths"},
    "SessionStart": {"cwd", "hookEventName", "workspacePaths"},
    "PreInvocation": {"cwd", "hookEventName", "workspacePaths"},
}
#: Every ``toolCall`` field the hook looks up, present or not (the
#: normalizer lifts ``name``/``args``/``id`` and probes the result keys a
#: PostToolUse payload might carry). Compared for equality.
TOOL_CALL_READS = {"name", "args", "id", "result", "output", "response"}
#: Reads that happen only on some calls: a read_file outside the workspace
#: also reads ``artifactDirectoryPath`` (to locate agy's MCP schema folder).
HOOK_READS_OUTSIDE_READ = HOOK_READS["PreToolUse"] | {"artifactDirectoryPath"}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def env(tmp_path):
    """A real workspace for the fixture paths, a home with no agy permission
    rules, and a private ctx state directory."""
    ws = tmp_path / "project"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / "a.txt").write_text("".join(f"line {i} of a.txt\n" for i in range(1, 11)))
    (ws / "big.log").write_text("".join(f"{i:06d} INFO worker ok\n" for i in range(20000)))
    home = tmp_path / "home"
    home.mkdir()
    return {"ws": ws, "home": home, "state": tmp_path / "state"}


def _materialize(payload: dict, env: dict) -> dict:
    text = json.dumps(payload)
    text = text.replace("/workspace/project", str(env["ws"])).replace("/home/user", str(env["home"]))
    return json.loads(text)


def _replay(stage: str, payload: dict, env: dict) -> dict:
    """``ctx hook antigravity <stage>`` as a process, the way agy runs it."""
    proc_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(env["home"]),
        "CTX_STATE_HOME": str(env["state"]),
        "PYTHONPATH": str(ROOT / "src"),
    }
    p = subprocess.run([sys.executable, "-m", "ctx", "hook", "antigravity", stage],
                       input=json.dumps(payload), capture_output=True, text=True,
                       env=proc_env, cwd=str(env["ws"]), timeout=60)
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, p.stdout  # exactly one JSON object on every path
    out = json.loads(lines[0])
    assert isinstance(out, dict)
    return out


def _with_args(payload: dict, **args) -> dict:
    out = json.loads(json.dumps(payload))
    out["toolCall"]["args"] = {**{k: v for k, v in out["toolCall"]["args"].items()
                                  if k not in ("Path", "CommandLine")}, **args}
    return out


# ------------------------------------------------------------ the fixtures
def test_fixtures_are_sanitized_and_complete():
    names = sorted(p.name for p in FIXTURES.glob("*.json"))
    assert names == sorted(FIXTURE_EVENTS)
    for name, (event, _stage) in FIXTURE_EVENTS.items():
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert "/tmp/" not in text and "claude" not in text.lower(), name
        data = json.loads(text)
        assert data["hookEventName"] == event
        assert data["conversationId"] == "00000000-0000-4000-8000-000000000000"
        assert data["workspacePaths"] == ["/workspace/project"]


# ------------------------------------------------------------- PreToolUse
def test_replayed_small_command_is_allowed_explicitly(env):
    out = _replay("pre-tool-use", _materialize(_load("pre_tool_use_run_command.json"), env), env)
    assert out == {"decision": "allow"}  # never {}: this host reads {} as a deny


def test_replayed_flooding_command_is_rewritten_in_place(env):
    payload = _with_args(_materialize(_load("pre_tool_use_run_command.json"), env),
                         CommandLine="cat big.log")
    out = _replay("pre-tool-use", payload, env)
    assert out["decision"] == "allow", out
    # only the changed argument is sent; the host merges it into the args
    assert set(out["overwrite"]) == {"CommandLine"}
    assert out["overwrite"]["CommandLine"].startswith("ctx run")
    # The original is carried verbatim, shell-quoted (ctx run --shell).
    assert shlex.split(out["overwrite"]["CommandLine"])[-1] == "cat big.log"


@pytest.mark.parametrize("command", [
    "nl a.txt", "seq 1 50000", "sort a.txt", "cut -d' ' -f2 a.txt", "base64 a.txt",
    "git ls-files", "git ls-tree -r HEAD", "git rev-list HEAD", "git grep -n line",
    "ls */*",
])
def test_replayed_text_reader_and_listing_are_rewritten_not_asked(env, command):
    """Measured in the 182-command replay: these were asked (a headless run
    ends with no answer) or ran natively and flooded."""
    out = _replay("pre-tool-use", _flood(env, command), env)
    assert out["decision"] == "allow", out
    assert set(out["overwrite"]) == {"CommandLine"}
    assert shlex.split(out["overwrite"]["CommandLine"])[-1] == command


@pytest.mark.parametrize("command,decision", [
    ("git rev-list --count HEAD", "allow"), ("git ls-tree HEAD", "allow"), ("ls -la", "allow"),
    ("git ls-files | wc -l", "allow"),           # a pipeline keeps its verdict
    ("sort -o out.txt a.txt", "force_ask"),       # writes a file: an unknown mutator
    ("base64 -o out a.txt | head -3", "force_ask"),  # a writer is not vouched as a part
    ("yes | head -n 5", "force_ask"),
])
def test_replayed_bounded_writing_and_piped_forms(env, command, decision):
    out = _replay("pre-tool-use", _flood(env, command), env)
    assert out["decision"] == decision and "overwrite" not in out, out


@pytest.mark.parametrize("command", [
    "sort --out=o.txt a.txt", "base64 --out=x a.txt", "sort -S 1k --compress-program=./ev.sh a.txt",
    "sort --files0-from=list", "./sort a.txt", "LD_PRELOAD=./x.so sort a.txt",
    "sort ~/.netrc", "cut -c1 /proc/1/environ", "base64 /dev/urandom", "fold -w1 /dev/zero",
    "seq inf", "seq 1 1000000000", "git for-each-ref --count=0",
])
def test_replayed_reader_that_writes_runs_or_reads_outside_is_asked(env, command):
    out = _replay("pre-tool-use", _flood(env, command), env)
    if command.startswith("git"):
        assert out["decision"] == "allow" and "overwrite" in out, out  # 0 = unlimited
    else:
        assert out["decision"] == "force_ask" and "overwrite" not in out, out


@pytest.mark.parametrize("command,kind", [("seq 1 5", "native"), ("seq 1 5000", "captured"),
                                          ("base64 a.txt | head -3", "captured"),
                                          ("git grep -e -q line", "captured")])
def test_replayed_seq_classes_and_base64_part(env, command, kind):
    out = _replay("pre-tool-use", _flood(env, command), env)
    assert out["decision"] == "allow", out
    assert ("overwrite" in out) == (kind == "captured"), out


@pytest.mark.parametrize("command,captured", [
    ("sort lnk", False), ("sort *.txt", False), ("sort ~user/x", False),
    # metacharacters keep main's verdict: captured through the compound/meta path
    ("sort {a.txt,/etc/passwd}", True), ("sort $HOME/x", True), ("sort < a.txt", True),
    ("sort < /etc/passwd", True),
    ("sort -T/etc a.txt", False), ("seq -s xx 1 400", False),
    ("sort inlnk", True), ("cut -d ',' -f2 a.txt", True), ("sort -t ',' -k2 a.txt", True),
    ("cut -d' ' -f1 a.txt", True),
])
def test_replayed_reader_captured_only_when_its_inputs_are_visible(env, command, captured):
    (env["ws"] / "lnk").symlink_to("/etc/passwd")
    (env["ws"] / "inlnk").symlink_to("a.txt")
    out = _replay("pre-tool-use", _flood(env, command), env)
    if captured:
        assert out["decision"] == "allow" and "overwrite" in out, out
    else:
        assert out["decision"] == "force_ask" and "overwrite" not in out, out


def test_replayed_text_reader_rewrite_respects_a_deny_rule(env):
    _rules(env, allow=["command(ctx)"], deny=["command(sort)"])
    out = _replay("pre-tool-use", _flood(env, "sort a.txt"), env)
    assert out["decision"] == "deny" and "overwrite" not in out


def test_replayed_large_read_file_is_bounded_in_place(env):
    payload = _with_args(_materialize(_load("pre_tool_use_read_file.json"), env),
                         Path=str(env["ws"] / "big.log"))
    out = _replay("pre-tool-use", payload, env)
    assert out["decision"] == "allow", out
    assert set(out["overwrite"]) == {"StartLine", "EndLine"}
    assert out["overwrite"]["StartLine"] == 1 and 1 <= out["overwrite"]["EndLine"] <= 240
    assert "ctx get repo:big.log" in out["reason"]


@pytest.mark.parametrize("start,end", [(3000, 5000), (4000, None), (2, 300), (2, 200), (1, None),
                                       (4990, 5000), (None, 400)])
def test_a_bounded_read_stays_inside_the_requested_range(env, start, end):
    """The host shows the model only "Showing lines A-B of N", never the
    hook's reason, so a rewrite that moved the window would be silent."""
    (env["ws"] / "wide.log").write_text("".join(f"{i:05d} " + "x" * 94 + "\n" for i in range(1, 5001)))
    args = {"Path": str(env["ws"] / "wide.log")}
    if start is not None:
        args["StartLine"] = start
    if end is not None:
        args["EndLine"] = end
    out = _replay("pre-tool-use", _with_args(_materialize(_load("pre_tool_use_read_file.json"), env),
                                             **args), env)
    if "overwrite" not in out:
        assert out["decision"] in ("allow", "deny"), out
        assert start == 4990 and out["decision"] == "allow"  # 11 lines fit: untouched
        return
    ow = out["overwrite"]
    first = start or 1
    assert out["decision"] == "allow"
    assert ow.get("StartLine", first) == first, out
    assert first <= ow["EndLine"] <= (end if end is not None else 5000), out
    assert (ow["EndLine"] - first + 1) * 101 <= 16384, out


@pytest.mark.parametrize("start,end", [(-5, 10), (0, 10), (10, 5), (True, 10), ("3", 10)])
def test_an_invalid_range_is_never_rewritten(env, start, end):
    (env["ws"] / "wide.log").write_text("".join(f"{i:05d} " + "x" * 94 + "\n" for i in range(1, 5001)))
    out = _replay("pre-tool-use", _with_args(_materialize(_load("pre_tool_use_read_file.json"), env),
                                             Path=str(env["ws"] / "wide.log"),
                                             StartLine=start, EndLine=end), env)
    # never a rewrite; a small invalid range may pass to the host untouched
    assert "overwrite" not in out, out
    assert out["decision"] == "deny" or (start, end) == (0, 10), out


def test_replayed_small_read_file_is_untouched(env):
    out = _replay("pre-tool-use", _materialize(_load("pre_tool_use_read_file.json"), env), env)
    assert out == {"decision": "allow"}


def test_unverified_tool_keeps_deny_and_steer(env):
    """view_file's overwrite was never observed live, so it is not rewritten."""
    payload = _materialize(_load("pre_tool_use_read_file.json"), env)
    payload["toolCall"] = {"name": "view_file", "id": "call_0000000000000000001",
                           "args": {"AbsolutePath": str(env["ws"] / "big.log")}}
    out = _replay("pre-tool-use", payload, env)
    assert out["decision"] == "deny" and "overwrite" not in out, out


def test_pre_1_5_4_payload_is_never_rewritten(env):
    """No hookEventName means an agy older than 1.5.4: deny and name the command."""
    payload = _with_args(_materialize(_load("pre_tool_use_run_command.json"), env),
                         CommandLine="cat big.log")
    del payload["hookEventName"]
    out = _replay("pre-tool-use", payload, env)
    assert out["decision"] == "deny" and "overwrite" not in out
    assert "Re-run it as: ctx run" in out["reason"]


def test_mcp_schema_read_outside_the_workspace_passes(env):
    """agy loads an MCP tool by reading <appDataDir>/mcp/<server>/<tool>.json
    (observed live with the ctx server); asking for it blocked every MCP tool
    in a headless run. Any other read outside the workspace still asks."""
    payload = _materialize(_load("pre_tool_use_read_file.json"), env)
    schema = env["home"] / ".gemini" / "antigravity-cli" / "mcp" / "ctx-harness_ctx-harness" / "ctx.json"
    schema.parent.mkdir(parents=True)
    schema.write_text('{"name": "ctx"}')
    out = _replay("pre-tool-use", _with_args(payload, Path=str(schema)), env)
    assert out == {"decision": "allow"}
    other = env["home"] / ".gemini" / "antigravity-cli" / "settings.json"
    other.write_text("{}")
    assert _replay("pre-tool-use", _with_args(payload, Path=str(other)), env)["decision"] == "force_ask"


@pytest.mark.parametrize("rel", [
    "srv/id_ed25519.json", "srv/credentials.json", "srv/token.json", "srv/.aws/c.json",
    "srv/.hidden.json", ".srv/t.json", "srv/sub/t.json", "t.json", "srv/t.txt", "srv/t.json.bak",
    "../mcp-evil/srv/t.json", "srv/../../settings.json",
])
def test_mcp_schema_exception_is_narrow(env, rel):
    """Only <server>/<tool>.json passes, and never a secret-looking name."""
    payload = _materialize(_load("pre_tool_use_read_file.json"), env)
    mcp = env["home"] / ".gemini" / "antigravity-cli" / "mcp"
    target = Path(os.path.normpath(mcp / rel))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")
    out = _replay("pre-tool-use", _with_args(payload, Path=str(mcp / rel)), env)
    assert out["decision"] == "force_ask", (rel, out)


def test_mcp_schema_exception_resolves_symlinks(env):
    payload = _materialize(_load("pre_tool_use_read_file.json"), env)
    mcp = env["home"] / ".gemini" / "antigravity-cli" / "mcp" / "srv"
    mcp.mkdir(parents=True)
    outside = env["home"] / "private.json"
    outside.write_text("{}")
    (mcp / "t.json").symlink_to(outside)
    out = _replay("pre-tool-use", _with_args(payload, Path=str(mcp / "t.json")), env)
    assert out["decision"] == "force_ask", out


def test_secret_read_still_asks(env):
    (env["ws"] / ".env").write_text("TOKEN=x\n")
    payload = _with_args(_materialize(_load("pre_tool_use_read_file.json"), env),
                         Path=str(env["ws"] / ".env"))
    out = _replay("pre-tool-use", payload, env)
    assert out["decision"] == "force_ask" and "overwrite" not in out


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '{"hookEventName": "PreToolUse"}'])
def test_pre_tool_use_never_answers_bare_object(env, stdin):
    proc_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(env["home"]),
                "CTX_STATE_HOME": str(env["state"]), "PYTHONPATH": str(ROOT / "src")}
    p = subprocess.run([sys.executable, "-m", "ctx", "hook", "antigravity", "pre-tool-use"],
                       input=stdin, capture_output=True, text=True, env=proc_env,
                       cwd=str(env["ws"]), timeout=60)
    out = json.loads(p.stdout)
    assert out.get("decision") in ("allow", "deny", "force_ask"), out


def test_read_budget_is_charged_to_the_conversation(env):
    """The read ledger is per session; on this host the session is
    ``conversationId`` (there is no session_id)."""
    payload = _materialize(_load("pre_tool_use_read_file.json"), env)
    payload["conversationId"] = "conv-ledger-probe"
    _replay("pre-tool-use", payload, env)
    ledger = env["ws"] / ".ctx-session-reads"
    assert (ledger / "conv-ledger-probe.count").is_file(), sorted(os.listdir(ledger))


def test_last_resort_answer_when_the_error_path_also_fails(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env["home"]))
    monkeypatch.setenv("CTX_STATE_HOME", str(env["state"]))

    def boom(*_a, **_k):
        raise RuntimeError("exploded")

    monkeypatch.setattr(hook, "_agy_overwrite", boom)
    monkeypatch.setattr(hook, "_internal_error_decision", boom)
    payload = _with_args(_materialize(_load("pre_tool_use_run_command.json"), env),
                         CommandLine="cat big.log")
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use("antigravity")
        text = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    assert json.loads(text) == {"decision": "allow"}


def test_emitter_failure_still_names_a_decision(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env["home"]))
    monkeypatch.setenv("CTX_STATE_HOME", str(env["state"]))

    def boom(*_a, **_k):
        raise RuntimeError("emitter exploded")

    monkeypatch.setattr(hook, "_agy_overwrite", boom)
    payload = _with_args(_materialize(_load("pre_tool_use_run_command.json"), env),
                         CommandLine="cat big.log")
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use("antigravity")
        out = json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    assert out["decision"] in ("allow", "force_ask", "deny") and "overwrite" not in out


# --------------------------------------------------- permission rules kept
def _rules(env: dict, **lists) -> None:
    path = env["home"] / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {k: v for k, v in lists.items()}}))


def _flood(env: dict, command: str = "cat big.log") -> dict:
    return _with_args(_materialize(_load("pre_tool_use_run_command.json"), env),
                      CommandLine=command)


def test_rewrite_is_asked_when_only_ctx_is_preapproved(env):
    """agy checks its rules against the rewritten `ctx run ...` line, so a
    rule approving ctx would silently approve a command the user's rules do
    not cover. The hook asks instead."""
    _rules(env, allow=["command(ctx)"])
    out = _replay("pre-tool-use", _flood(env), env)
    assert out["decision"] == "force_ask", out
    assert out["overwrite"]["CommandLine"].startswith("ctx run")


def test_rewrite_is_allowed_when_the_original_is_allowed_too(env):
    _rules(env, allow=["command(ctx)", "command(cat)"])
    out = _replay("pre-tool-use", _flood(env), env)
    assert out["decision"] == "allow" and "overwrite" in out


def test_rewrite_without_a_ctx_rule_defers_to_the_host(env):
    """No rule covers `ctx run`: the host itself asks (headless: refuses)."""
    _rules(env, allow=["command(echo)"])
    out = _replay("pre-tool-use", _flood(env), env)
    assert out["decision"] == "allow" and "overwrite" in out


def test_a_deny_rule_on_the_original_is_kept(env):
    _rules(env, allow=["command(ctx)"], deny=["command(cat)"])
    out = _replay("pre-tool-use", _flood(env), env)
    assert out["decision"] == "deny" and "overwrite" not in out


def test_an_ask_rule_on_a_compound_segment_is_kept(env):
    _rules(env, allow=["command(ctx)", "command(cat)", "command(echo)"], ask=["command(cat)"])
    out = _replay("pre-tool-use", _flood(env, "echo start && cat big.log"), env)
    assert out["decision"] in ("force_ask", "deny"), out


def test_unreadable_rules_ask(env):
    path = env["home"] / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    out = _replay("pre-tool-use", _flood(env), env)
    assert out["decision"] == "force_ask"


# --------------------------------------------- PostToolUse and the others
@pytest.mark.parametrize("name", ["post_tool_use_run_command.json", "post_tool_use_read_file.json"])
def test_replayed_post_tool_use_answers_the_only_legal_output(env, name):
    assert _replay("post-tool-use", _materialize(_load(name), env), env) == {}


def test_replayed_session_start_and_pre_invocation_are_quiet_when_nothing_is_pending(env):
    assert _replay("session-start", _materialize(_load("session_start.json"), env), env) == {}
    assert _replay("pre-invocation", _materialize(_load("pre_invocation.json"), env), env) == {}


@pytest.mark.parametrize("name,stage", [("stop.json", "stop"), ("post_invocation.json", "post-invocation")])
def test_unregistered_events_never_hold_the_session(env, name, stage):
    """ctx registers neither Stop nor PostInvocation; if one were routed to it
    anyway, the answer must not keep the agent running (Stop: only
    "continue" re-enters the loop; PostInvocation: no terminationBehavior)."""
    out = _replay(stage, _materialize(_load(name), env), env)
    assert out.get("decision") != "continue" and "terminationBehavior" not in out


def test_plugin_registers_only_events_ctx_answers():
    hooks = json.loads((ROOT / "plugins" / "antigravity" / "hooks.json").read_text())["ctx-harness"]
    registered = {k for k in hooks if k != "enabled"}
    assert registered == {"PreToolUse", "PostToolUse", "SessionStart", "PreInvocation"}
    # agy 1.5.5 rejects the whole file if an event without a matcher nests
    # its handlers under "hooks" ("command hook must specify 'command'").
    for event in ("SessionStart", "PreInvocation"):
        for handler in hooks[event]:
            assert set(handler) <= {"type", "command", "timeout"}, handler
            assert handler["command"].endswith(
                "hook antigravity " + FIXTURE_EVENTS[
                    "session_start.json" if event == "SessionStart" else "pre_invocation.json"][1])
    for event in ("PreToolUse", "PostToolUse"):
        for group in hooks[event]:
            assert set(group) == {"matcher", "hooks"}


def _run_session(stage_payload: dict, monkeypatch, env, advisory: str, relay: str) -> tuple[dict, list]:
    monkeypatch.setenv("HOME", str(env["home"]))
    monkeypatch.setenv("CTX_STATE_HOME", str(env["state"]))
    calls: list = []
    from ctx import surface

    def fake_preflight(*a, **k):
        calls.append("preflight")
        return advisory

    monkeypatch.setattr(surface, "preflight", fake_preflight)
    monkeypatch.setattr(hook, "_relay_drain", lambda p, f, stage, *a: (calls.append(stage) or relay, []))
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(stage_payload)), io.StringIO()
    try:
        hook.main_session_start("antigravity")
        out = json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return out, calls


def test_session_start_carries_the_advisory_once_as_a_system_message(env, monkeypatch):
    out, calls = _run_session(_materialize(_load("session_start.json"), env),
                              monkeypatch, env, "SURFACE ADVISORY", "")
    assert out == {"injectSteps": [{"systemMessage": {"systemMessage": "SURFACE ADVISORY"}}]}
    assert calls == ["preflight", "session-start"]


def test_pre_invocation_carries_only_the_relay_ephemerally(env, monkeypatch):
    out, calls = _run_session(_materialize(_load("pre_invocation.json"), env),
                              monkeypatch, env, "SURFACE ADVISORY", "RELAY NOTE")
    assert out == {"injectSteps": [{"ephemeralMessage": "RELAY NOTE"}]}
    assert calls == ["pre-invocation"]  # no pre-flight before every model call


def test_pre_1_5_4_pre_invocation_keeps_the_advisory(env, monkeypatch):
    payload = _materialize(_load("pre_invocation.json"), env)
    del payload["hookEventName"]
    out, calls = _run_session(payload, monkeypatch, env, "SURFACE ADVISORY", "")
    assert out == {"injectSteps": [{"ephemeralMessage": "SURFACE ADVISORY"}]}
    assert calls == ["preflight", "session-start"]


# ------------------------------------------------- payload key drift guard
class _Tracking(dict):
    """A payload that records which keys the hook looks at."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen: set[str] = set()

    def get(self, key, default=None):
        self.seen.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.seen.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.seen.add(key)
        return super().__contains__(key)


def _measured_reads(name: str, env: dict, monkeypatch, args: dict | None = None
                    ) -> tuple[set[str], set[str]]:
    event, _stage = FIXTURE_EVENTS[name]
    raw = _materialize(_load(name), env)
    if args is not None:
        raw["toolCall"]["args"] = args
    monkeypatch.setenv("HOME", str(env["home"]))
    monkeypatch.setenv("CTX_STATE_HOME", str(env["state"]))
    real = hook._normalize_payload
    box: dict = {}

    def tracked(payload):
        call = payload.get("toolCall") if isinstance(payload, dict) else None
        if isinstance(call, dict):
            payload = dict(payload, toolCall=_Tracking(call))
            box["call"] = payload["toolCall"]
        out = _Tracking(real(payload))
        box["top"] = out
        return out

    monkeypatch.setattr(hook, "_normalize_payload", tracked)
    entry = {"PreToolUse": hook.main_pre_tool_use, "PostToolUse": hook.main_post_tool_use,
             "SessionStart": hook.main_session_start,
             "PreInvocation": hook.main_session_start}[event]
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(raw)), io.StringIO()
    try:
        entry("antigravity")
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    keys = set(raw)
    top = box["top"].seen & keys
    if "call" in box:
        top.add("toolCall")
    return top, (box["call"].seen if "call" in box else set())


@pytest.mark.parametrize("name", [n for n, (_e, s) in FIXTURE_EVENTS.items() if s])
def test_hook_reads_exactly_the_declared_keys(name, env, monkeypatch):
    """Fails when the real payload and what the hook reads drift apart: a key
    the hook depends on disappears or is renamed in a re-captured fixture, or
    the hook starts (or stops) reading one without this table saying so."""
    event, _stage = FIXTURE_EVENTS[name]
    fixture_keys = set(_load(name))
    assert HOOK_READS[event] <= fixture_keys, (
        f"the hook reads {sorted(HOOK_READS[event] - fixture_keys)} which agy does not send")
    top, call = _measured_reads(name, env, monkeypatch)
    assert top == HOOK_READS[event], (name, sorted(top))
    if "toolCall" in fixture_keys:
        assert {"name", "args", "id"} <= set(_load(name)["toolCall"])
        assert call == TOOL_CALL_READS, (name, sorted(call))


def test_conditional_reads_are_declared(env, monkeypatch):
    """A read_file outside the workspace reads artifactDirectoryPath too."""
    assert "artifactDirectoryPath" in _load("pre_tool_use_read_file.json")
    outside = env["home"] / "elsewhere.txt"
    outside.write_text("x\n")
    top, _call = _measured_reads("pre_tool_use_read_file.json", env, monkeypatch,
                                 args={"Path": str(outside)})
    assert top == HOOK_READS_OUTSIDE_READ, sorted(top)
