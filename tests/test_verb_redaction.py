"""Every verb and MCP op that can print workspace or artifact content passes
the model-visible pipeline (redaction) before it prints.

A secret is planted in a workspace file and in a stored artifact; each
content-bearing CLI verb (text and, where it has one, ``--json`` form) and
each content-bearing MCP op runs over it, and no run of the secret's random
part longer than a few characters may appear in what it prints.

Exempt verbs (they print no workspace or artifact content) are listed in
``EXEMPT`` with the reason; the test also checks that every verb of the
command table is either covered or exempt, so a new verb must choose.
"""

from __future__ import annotations

import json
import os
import random
import re
import string
import subprocess
import sys

import pytest

FRAG = 5
_R = random.Random(41)


def _rand(n, alphabet=string.ascii_letters + string.digits):
    return "".join(_R.choice(alphabet) for _ in range(n))


SECRETS = {
    "github-token": "ghp_" + _rand(36),
    "anthropic-key": "sk-ant-" + _rand(30),
    "aws-access-key": "AKIA" + _rand(16, string.ascii_uppercase + string.digits),
    "jwt": "eyJ" + _rand(24) + "." + _rand(12) + "." + _rand(14),
}
BODIES = [s.split("_", 1)[-1] if s.startswith("ghp_") else s[7:] if s.startswith("sk-ant-")
          else s[4:] if s.startswith("AKIA") else s[3:] for s in SECRETS.values()]

_KEY_ALPHA = string.ascii_letters + string.digits + "+/"
KEY1 = [_rand(64, _KEY_ALPHA) for _ in range(8)]
KEY2 = [_rand(64, _KEY_ALPHA) for _ in range(8)]


def _pem(lines):
    return "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END RSA PRIVATE KEY-----"


SOURCE = f'''class Base:
    pass


class Impl(Base):
    pass


def helper(y):
    key = "{SECRETS["anthropic-key"]}"
    return y


def target_function(x):
    return helper(x)  # token {SECRETS["github-token"]}


AWS = "{SECRETS["aws-access-key"]}"
JWT = "{SECRETS["jwt"]}"
value = target_function(2)  # {SECRETS["github-token"]}
LONG = "{"w" * 140} {SECRETS["github-token"]} tail"
PEM = """
{_pem(KEY1)}
"""
'''

#: verb -> why it is not run here (each prints no workspace, artifact or
#: stored content, or passes the pipeline on a path named in the reason).
#:
#: NON-GOAL (stated once, in ctx.textutil's docstring): an argument echoed
#: back within the same call to the caller who supplied it is not redacted --
#: the caller already holds it, and a suggested command must be the exact
#: command. The hook's ask/deny reasons, `ask`'s "subject:", `plan run` /
#: MCP `investigate`'s "objective:" and `checkpoint`'s bad --evidence are
#: such echoes. Text STORED by one call and shown by a later one is content:
#: debt, collab, task and relay pass the pipeline and are tested below.
EXEMPT = {
    "image": "image/PDF structure digest: format, dimensions, hashes -- no text content",
    "lsp": "needs a language server; its JSON and site/hover output pass the pipeline "
           "(commands/retrieve.py cmd_lsp: redact_strings / model_visible)",
    "semantic": "calls a model; prints through _emit (sanitize_for_model)",
    "surface": "the agent's tool/skill surface from host config, not workspace or artifacts",
    "prune": "defers host tools/skills; prints their names",
    "gain": "token accounting numbers",
    "init": "writes ctx.toml / .ctxignore; prints paths",
    "doctor": "environment checks",
    "index": "builds the code index; prints counts",
    "gc": "prints counts",
    "pin": "prints the pinned id",
    "checkpoint": "echoes the caller's own --goal/--evidence within the same call (non-goal)",
    "policy": "compiled command-steering rules (command signatures)",
    "ladders": "escalation telemetry",
    "coverage": "host/tool coverage table",
    "orchestrate": "spawns agents (not run in tests); its status text passes model_visible",
    "setup": "host installation",
    "replay": "reads host transcripts, not this workspace or its artifacts",
    "wrap": "host launcher",
    "agent": "runs a model-hosted agent; not run in tests",
    "antigravity": "host plugin installer",
    "proxy": "API traffic accounting",
    "rewrite": "prints file names, edit counts and a blob handle; the diff itself is read "
               "with `ctx get`, covered here",
    "plan": "plan ops are the retrieval verbs covered here; 'objective:' echoes the caller (non-goal)",
    "ask": "answers from the retrieval verbs covered here; 'subject:' echoes the caller (non-goal)",
    "jobs": "lists job ids, status and argv (argv clipped redaction-aware)",
    "view": "media files (images, video, subtitles); text subtitles covered by media tests",
}


def _key_runs(lines) -> set[str]:
    """Runs of 8+ non-digit characters of a key's body lines: what survives
    any digit masking (a log template shows ``ab<*>cd``)."""
    runs = set()
    for ln in lines:
        runs.update(r for r in re.split(r"[0-9]+", ln) if len(r) >= 8)
    return runs


KEY_RUNS = _key_runs(KEY1) | _key_runs(KEY2)


def _leaks(text: str) -> list[str]:
    out = [r for r in KEY_RUNS if r in text][:3]
    for body in BODIES:
        for part in body.split("."):
            for i in range(len(part) - FRAG):
                if part[i:i + FRAG + 1] in text:
                    out.append(part[i:i + FRAG + 1])
                    break
    return out


@pytest.fixture(scope="module")
def ws_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("verbs")
    ws = root / "ws"
    ws.mkdir()
    state = root / "state"
    (ws / "ctx.toml").write_text("version = 1\n")
    (ws / "app.py").write_text(SOURCE)
    env = {**os.environ, "CTX_STATE_HOME": str(state), "HOME": str(root / "home"),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    env.pop("CTX_CONVERSATION_ID", None)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)

    def ctx(*args, stdin: bytes | None = None) -> str:
        p = subprocess.run([sys.executable, "-P", "-m", "ctx", "--workspace", str(ws), *args],
                           cwd=ws, env=env, input=stdin, capture_output=True, timeout=120)
        return p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")

    first = ctx("run", "--", "cat", "app.py")
    run_a = re.search(r"\[ctx (run:[0-9a-f]+)", first).group(1)
    (ws / "app2.py").write_text(SOURCE.replace("pass", "pass  # changed")
                                + f'PEM2 = """\n{_pem(KEY2)}\n"""\n')
    second = ctx("run", "--", "cat", "app2.py")
    run_b = re.search(r"\[ctx (run:[0-9a-f]+)", second).group(1)
    return {"ctx": ctx, "ws": ws, "env": env, "run_a": run_a, "run_b": run_b,
            "outputs": {"run": first, "run2": second}}


KEY_LINE = SOURCE.splitlines().index(KEY1[0]) + 1  # first key body line in app.py


def _cases(e):
    a, b = e["run_a"], e["run_b"]
    k = KEY_LINE
    return {
        # views that start inside, or quote around, the private-key block
        "search repo key": ["search", "repo:", "PRIVATE KEY", "--context", "4"],
        "search run key": ["search", a, "PRIVATE KEY", "--context", "4"],
        "get repo in key": ["get", "repo:app.py", "--lines", f"{k + 2}:{k + 9}"],
        "get run in key": ["get", f"{a}#stdout", "--lines", f"{k + 2}:{k + 9}"],
        "q search key": ["q", "search 'PRIVATE KEY' app.py"],
        "stats repo outline": ["stats", "repo:app.py"],
        "run": ["run", "--", "cat", "app.py"],
        "run --json": ["run", "--json", "--", "cat", "app.py"],
        "run --passthrough": ["run", "--passthrough", "--", "cat", "app.py"],
        "py": ["py", "print(open('app.py').read())"],
        "seq": ["seq", "--step", "cat app.py"],
        "search repo": ["search", "repo:", "target_function", "--context", "2"],
        "search run": ["search", a, "helper", "--context", "3"],
        "get repo": ["get", "repo:app.py"],
        "get run lines": ["get", f"{a}#stdout", "--lines", "1:40"],
        "get run bytes": ["get", f"{a}#stdout", "--bytes", "1:900"],
        "stats repo": ["stats", "repo:app.py"],
        "stats run": ["stats", a],
        "diff": ["diff", a, b],
        "intake": ["intake", "app.py"],
        "intake --json": ["intake", "app.py", "--json"],
        "map": ["map"],
        "def": ["def", "repo:app.py:target_function"],
        "refs": ["refs", "target_function"],
        "diag": ["diag", "app.py"],
        "callers": ["callers", "helper"],
        "callees": ["callees", "target_function"],
        "impact": ["impact", "helper"],
        "impls": ["impls", "Base"],
        "cycles": ["cycles"],
        "q search": ["q", "search target_function app.py"],
        "q get": ["q", "search target_function app.py | get"],
        "pack": ["pack", "target_function helper"],
        "pack --json": ["pack", "--json", "target_function helper"],
        "compress": ["compress", "app.py", "--query", "helper target_function"],
    }


#: cases whose output legitimately quotes no source text (counts, shapes)
NO_CONTENT_EXPECTED = {"diff", "stats repo", "stats run", "cycles", "diag", "map",
                       "search repo key", "search run key", "get repo in key", "get run in key",
                       "q search key", "stats repo outline"}


@pytest.mark.parametrize("name", sorted(_cases({"run_a": "run:x", "run_b": "run:y"})))
def test_content_verb_output_is_redacted(ws_env, name):
    args = _cases(ws_env)[name]
    out = ws_env["ctx"](*args)
    assert not _leaks(out), (name, _leaks(out), out[:1500])
    # the verb really printed workspace / artifact content (not an error)
    if name not in NO_CONTENT_EXPECTED:
        assert any(w in out for w in ("target_function", "helper", "Base", "AWS")), (name, out[:800])


def test_job_output_is_redacted(ws_env):
    out = ws_env["ctx"]("run", "--bg", "--", "cat", "app.py")
    m = re.search(r"\bjob[: ]+([0-9a-f]{6,})", out) or re.search(r"(job_[0-9a-f]+|[0-9a-f]{12})", out)
    assert m, out
    tail = ws_env["ctx"]("job", m.group(1), "--wait", "--tail", "40")
    assert not _leaks(out + tail), (out + tail)[:1500]


def test_edit_replace_preview_is_redacted(ws_env):
    ws = ws_env["ws"]
    (ws / "rep.txt").write_text("    return helper(x)  # replaced\n")
    out = ws_env["ctx"]("edit", "replace", "repo:app.py", "--lines", "14:16",
                        "--replacement-file", "rep.txt")
    assert not _leaks(out), out[:1500]


def test_capsule_verify_is_redacted(ws_env):
    ws = ws_env["ws"]
    out = ws_env["ctx"]("capsule", "export", "cap.json", "--handle", ws_env["run_a"])
    out += ws_env["ctx"]("capsule", "verify", "cap.json")
    assert not _leaks(out), out[:1500]
    assert (ws / "cap.json").exists() or "cap" in out


MCP_OPS = {
    "search": {"op": "search", "ref": "repo:", "patterns": ["target_function"],
               "options": {"context": 2}},
    "search run": {"op": "search", "ref": None, "patterns": ["helper"], "options": {"context": 3}},
    "get": {"op": "get", "ref": "repo:app.py"},
    "get run": {"op": "get", "ref": None, "selector": {"lines": "1:40"}},
    "get run bytes": {"op": "get", "ref": None, "selector": {"bytes": "1:900"}},
    "stats": {"op": "stats", "ref": "repo:app.py"},
    "map": {"op": "map"},
    "def": {"op": "def", "options": {"target": "repo:app.py:target_function"}},
    "refs": {"op": "refs", "options": {"symbol": "target_function"}},
    "callers": {"op": "callers", "options": {"symbol": "helper"}},
    "callees": {"op": "callees", "options": {"symbol": "target_function"}},
    "diff": {"op": "diff", "options": {}},
    "q": {"op": "q", "options": {"pipeline": "search target_function app.py"}},
    "q get": {"op": "q", "options": {"pipeline": "search target_function app.py | get"}},
    "search key": {"op": "search", "ref": "repo:", "patterns": ["PRIVATE KEY"],
                   "options": {"context": 4}},
    "get in key": {"op": "get", "ref": "repo:app.py",
                   "selector": {"lines": f"{KEY_LINE + 2}:{KEY_LINE + 9}"}},
}
MCP_NO_CONTENT = {"stats", "map", "diff", "search key", "get in key"}


@pytest.mark.parametrize("name", sorted(MCP_OPS))
def test_mcp_op_output_is_redacted(ws_env, name, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", ws_env["env"]["CTX_STATE_HOME"])
    from ctx.mcp import dispatch_bounded
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(ws_env["ws"]))
    store = Store(ws.workspace_id)
    args = dict(MCP_OPS[name])
    if args.get("ref", "") is None:
        args["ref"] = ws_env["run_a"] + ("#stdout" if name.startswith("get") else "")
    if name == "diff":
        args["options"] = {"refA": ws_env["run_a"], "refB": ws_env["run_b"]}
    out = dispatch_bounded(ws, store, args)
    assert not _leaks(out), (name, out[:1500])
    if name not in MCP_NO_CONTENT:
        assert any(w in out for w in ("target_function", "helper", "Base", "AWS")), (name, out[:800])


def test_every_verb_is_covered_or_exempt():
    from ctx import cli

    covered = {c.split()[0] for c in _cases({"run_a": "x", "run_b": "y"})}
    covered |= {"job", "edit", "capsule", "debt", "collab", "task", "relay"}
    missing = set(cli._COMMANDS) - covered - set(EXEMPT)
    assert not missing, f"verbs neither covered nor exempt: {sorted(missing)}"


def test_hook_compound_message_quotes_a_command_redaction_aware(monkeypatch):
    """The guard quotes part of the user's command in its ask message; a
    command line can carry a credential straddling the 80-character cut."""
    from ctx import hook

    token = SECRETS["github-token"]
    seg = "deploy --verbose " + "x" * 50 + " --token " + token
    monkeypatch.setattr(hook, "classify_command",
                        lambda *a, **k: {"decision": "force_ask", "reason": ""})
    monkeypatch.setattr(hook, "_part_program", lambda s: None)
    out = hook._compound_verdict(seg + " ; echo done", {}, 0, cwd=None)
    text = json.dumps(out)
    quoted = re.search(r"vouch for \((.*?)\), so", text).group(1)
    assert not _leaks(quoted), quoted
    off = hook._compound_verdict(seg + " ; echo done", {"redaction_enabled": False}, 0, cwd=None)
    off_quoted = re.search(r"vouch for \((.*?)\), so", json.dumps(off)).group(1)
    assert "ghp_" in off_quoted and "*" not in off_quoted  # redaction off means off


def test_stored_text_verbs_pass_the_pipeline(ws_env):
    """Text stored by one call and printed by a later one is content."""
    ctx = ws_env["ctx"]
    note = "remember " + SECRETS["github-token"] + " for later"
    ctx("debt", "add", note)
    out = ctx("debt", "list")
    assert "remember" in out and not _leaks(out), out[:800]
    ctx("collab", "new", "--goal", "rotate " + SECRETS["anthropic-key"])
    listed = ctx("collab", "list")
    assert "rotate" in listed and not _leaks(listed), listed[:800]
    task_id = re.search(r"^(\S+) \[", listed, re.M).group(1)
    shown = ctx("collab", "show", task_id)
    assert "rotate" in shown and not _leaks(shown), shown[:800]
    handoff = ctx("collab", "handoff", task_id, "--to", "claude")
    assert not _leaks(handoff), handoff[:800]
    ctx("task", "send", "task-red", "worker", ws_env["run_a"], "--note",
        "key " + SECRETS["aws-access-key"])
    inbox = ctx("task", "inbox", "task-red", "worker")
    assert "key" in inbox and not _leaks(inbox), inbox[:800]


def test_mcp_task_ops_pass_the_pipeline(ws_env, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", ws_env["env"]["CTX_STATE_HOME"])
    from ctx.mcp import dispatch_bounded
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(ws_env["ws"]))
    store = Store(ws.workspace_id)
    opts = {"task": "task-mcp", "node": "n1", "ref": ws_env["run_a"],
            "note": "token " + SECRETS["github-token"]}
    dispatch_bounded(ws, store, {"op": "send", "options": opts})
    inbox = dispatch_bounded(ws, store, {"op": "inbox", "options": {"task": "task-mcp", "node": "n1"}})
    assert "token" in inbox and not _leaks(inbox), inbox
    show = dispatch_bounded(ws, store, {"op": "task", "options": {"task": "task-mcp"}})
    assert not _leaks(show), show
