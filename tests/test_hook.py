"""Acceptance: hook JSON contract, classification policy, fail-open."""

import json
import shlex

import pytest
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _invoke_hook(payload: str, env_extra=None) -> dict:
    """Run the real hook entry end-to-end (stdin JSON → stdout JSON)."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", "ctx", "hook", "antigravity", "pre-tool-use"],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    assert len(lines) == 1, f"hook must emit exactly one JSON object, got: {proc.stdout!r}"
    return json.loads(lines[0])


def _classify(tool_name, tool_input, workspace=None):
    from ctx.hook import classify

    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if workspace:
        payload["workspacePaths"] = [str(workspace)]
    return classify(payload)


def _steering_deny(workspace):
    """Pin the workspace to the pure deny-with-remediation contract so the
    deny assertions below stay covered verbatim (steering off)."""
    (workspace / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )


def test_hook_emits_single_json_on_garbage_stdin():
    decision = _invoke_hook("this is not json {{{")
    assert decision == {"decision": "allow"}  # fail-open in default mode


def test_hook_emits_single_json_on_empty_stdin():
    decision = _invoke_hook("")
    assert decision == {"decision": "allow"}


def test_deny_unbounded_command_with_remediation(tmp_path):
    _steering_deny(tmp_path)
    d = _classify("run_command", {"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    assert d["decision"] == "deny"
    assert "ctx run -- pytest -q" in d["reason"]
    assert set(d) == {"decision", "reason"}  # no rewrite attached under steering=deny


def test_allow_ctx_routed_command(tmp_path):
    d = _classify("run_command", {"CommandLine": "ctx run -- pytest -q"}, tmp_path)
    assert d["decision"] == "allow"


def test_allow_bounded_commands(tmp_path):
    for cmd in ("pwd", "git status --short", "head -n 40 file.txt", "echo hi"):
        d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
        assert d["decision"] == "allow", cmd


def test_deny_unbounded_git_and_cat(tmp_path):
    _steering_deny(tmp_path)
    for cmd in ("git log", "git diff", "cat big.log", "find . -name '*.py'", "rg pattern"):
        d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
        assert d["decision"] == "deny", cmd


def test_pipeline_with_head_is_not_auto_safe(tmp_path):
    _steering_deny(tmp_path)
    d = _classify("run_command", {"CommandLine": "cat huge.log | head -n 5"}, tmp_path)
    assert d["decision"] == "force_ask"


def test_force_ask_secret_path(tmp_path):
    d = _classify("Read", {"file_path": str(tmp_path / ".env")}, tmp_path)
    assert d["decision"] == "force_ask"
    assert ".env" not in d["reason"] or "secret" in d["reason"].lower()


def test_force_ask_outside_workspace_read(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    d = _classify("Read", {"file_path": str(outside)}, ws)
    assert d["decision"] == "force_ask"


def test_deny_large_file_read(tmp_path):
    _steering_deny(tmp_path)
    big = tmp_path / "big.txt"
    big.write_text("x" * 20000, encoding="utf-8")
    d = _classify("Read", {"file_path": str(big)}, tmp_path)
    assert d["decision"] == "deny"
    assert "ctx get" in d["reason"]


def test_allow_small_file_read(tmp_path):
    small = tmp_path / "small.txt"
    small.write_text("hello", encoding="utf-8")
    d = _classify("Read", {"file_path": str(small)}, tmp_path)
    assert d["decision"] == "allow"


def test_longest_workspace_path_wins(tmp_path):
    from ctx.hook import _resolve_workspace_root

    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    root = _resolve_workspace_root(
        {
            "tool_input": {"Cwd": str(inner)},
            "workspacePaths": [str(outer), str(inner)],
        }
    )
    assert root == str(inner)


def test_multi_root_without_disambiguation_is_conservative(tmp_path):
    from ctx.hook import _resolve_workspace_root

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    root = _resolve_workspace_root({"tool_input": {}, "workspacePaths": [str(a), str(b)]})
    assert root is None  # no guess; policy falls back to defaults


def test_advisory_mode_allows_everything(tmp_path):
    (tmp_path / "ctx.toml").write_text('version = 1\n[guard]\nmode = "advisory"\n', encoding="utf-8")
    d = _classify("run_command", {"CommandLine": "pytest -q", "Cwd": str(tmp_path)}, tmp_path)
    assert d["decision"] == "allow"


def test_fail_closed_when_configured(tmp_path):
    (tmp_path / "ctx.toml").write_text(
        'version = 1\n[guard]\ninternal_error = "deny"\n', encoding="utf-8"
    )
    payload = json.dumps(
        {
            "tool_name": "run_command",
            "tool_input": {"CommandLine": None, "Cwd": str(tmp_path)},
            "workspacePaths": [str(tmp_path)],
        }
    )
    # CommandLine None → classify treats as empty command → allow; craft a
    # real internal error instead by making tool_input a bad type after parse.
    from ctx import hook

    original = hook.classify
    try:
        def boom(payload):
            raise RuntimeError("boom")

        hook.classify = boom
        import io
        import sys as _sys

        stdin, stdout = _sys.stdin, _sys.stdout
        _sys.stdin = io.StringIO(payload)
        _sys.stdout = io.StringIO()
        try:
            hook.main_pre_tool_use()
            out = _sys.stdout.getvalue()
        finally:
            _sys.stdin, _sys.stdout = stdin, stdout
    finally:
        hook.classify = original
    decision = json.loads(out)
    assert decision["decision"] == "deny"


def test_interpreter_bypass_channel_denied(tmp_path):
    d = _classify(
        "run_command",
        {"CommandLine": "python -c \"print(open('big.log').read())\"", "Cwd": str(tmp_path)},
        tmp_path,
    )
    assert d["decision"] in ("deny", "force_ask")


def test_claude_code_flavor_schema(tmp_path):
    import subprocess as sp

    _steering_deny(tmp_path)
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "cwd": str(tmp_path)}
    )
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = sp.run(
        [sys.executable, "-m", "ctx", "hook", "claude-code", "pre-tool-use"],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    assert len(lines) == 1
    out = json.loads(lines[0])
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "ctx run -- pytest -q" in hso["permissionDecisionReason"]


def test_claude_code_flavor_allow_is_valid():
    from ctx.hook import _to_claude_code_schema

    out = _to_claude_code_schema({"decision": "allow"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    out2 = _to_claude_code_schema({"decision": "force_ask", "reason": "why"})
    assert out2["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_codex_plain_allow_emits_no_unsupported_decision():
    from ctx.hook import _to_codex_schema

    assert _to_codex_schema({"decision": "allow"}) == {}


def test_codex_rewrite_is_withheld():
    """Codex takes a rewrite only as `allow` + `updatedInput`, and its docs do
    not establish that its approval policy survives a hook allow (on Claude
    Code the same shape skipped the user's deny and ask rules, live). So the
    original runs under Codex's own approval; the output gate bounds it."""
    from ctx.hook import _to_codex_schema

    out = _to_codex_schema(
        {
            "decision": "allow",
            "rewrite": {
                "updatedInput": {"command": "ctx run -- pytest -q"},
                "reason": "contain output",
            },
        }
    )
    assert out == {}


def test_codex_force_ask_degrades_safely_to_deny():
    from ctx.hook import _to_codex_schema

    out = _to_codex_schema({"decision": "force_ask", "reason": "review this"})
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "review this"


# ------------------------------------------------ container escape surfaces
# headlong's docker broker denylist, taken as a safety-class force_ask: a
# privileged container, a host namespace, a host device, the engine socket
# or a bind mount from outside the workspace reach past the confinement the
# rest of the guard enforces. One predicate, every door.
def _cmd(command, ws):
    return _classify("run_command", {"CommandLine": command, "Cwd": str(ws)}, ws)


@pytest.mark.parametrize("command", [
    "docker run --privileged img",
    "docker exec --privileged c sh",
    "docker run --network host img",
    "docker run --pid=host img",
    "podman run --uts=host img",
    "docker run --device /dev/sda img",
    "docker run --cap-add=SYS_ADMIN img",
    "docker run -v /:/host img",
    "docker run -v ../:/up img",
    "docker run -v /var/run/docker.sock:/var/run/docker.sock img",
    "docker run --mount type=bind,source=/etc,target=/x img",
])
def test_container_escape_surfaces_force_ask(tmp_path, command):
    d = _cmd(command, tmp_path)
    assert d["decision"] == "force_ask", (command, d)
    assert "container escape surface" in d["reason"]


def test_unresolved_shell_variable_mount_still_asks(tmp_path):
    """`$HOME` is a shell metacharacter, so the compound-expression door
    answers first; the predicate itself refuses to guess what the variable
    holds. Either way the user sees it."""
    from ctx.hook import _container_escape

    assert _cmd("docker run -v $HOME/.ssh:/root/.ssh img", tmp_path)["decision"] == "force_ask"
    why = _container_escape(["docker", "run", "-v", "$HOME/.ssh:/root/.ssh", "img"], str(tmp_path))
    assert why and "cannot resolve" in why
    assert _container_escape(["docker", "run", "-v", "$PWD/data:/data", "img"], str(tmp_path)) is None


@pytest.mark.parametrize("command", [
    "docker ps",
    "docker compose up",
    "docker run -v named:/data img",
    "docker run -v ./data:/data img",
    "docker run --cap-add NET_BIND_SERVICE img",
    "docker run --mount type=bind,source={ws}/data,target=/x img",
])
def test_in_workspace_container_use_keeps_its_volume_class_answer(tmp_path, command):
    (tmp_path / "data").mkdir()
    _steering_deny(tmp_path)
    d = _cmd(command.format(ws=tmp_path), tmp_path)
    assert d["decision"] == "deny", (command, d)  # unbounded output, ctx run remediation
    assert "container escape" not in d["reason"]


@pytest.mark.parametrize("command", [
    "docker run --privileged img > out.log 2>&1",       # the redirect shortcut
    "echo hi && docker run --privileged img",           # a chain segment
    "ctx run -- docker run --privileged img",           # the deny remediation's own door
    "ctx run --shell -- 'docker run --privileged img | head -5'",
])
def test_container_escape_is_seen_through_every_door(tmp_path, command):
    d = _cmd(command, tmp_path)
    assert d["decision"] == "force_ask", (command, d)
    assert "container escape surface" in d["reason"]


def test_ctx_run_door_keeps_the_secret_path_guarantee(tmp_path):
    """`ctx run -- cat secrets.json` used to be allowed outright because the
    program was `ctx`; capture answers the volume question, never the
    safety one."""
    assert _cmd("ctx run -- cat secrets.json", tmp_path)["decision"] == "force_ask"
    assert _cmd("ctx run -- pytest -q", tmp_path)["decision"] == "allow"


# ------------------------------------------------------- caps never widen
def test_large_file_read_keeps_the_callers_smaller_limit(tmp_path):
    """The large-file rewrite bounds a Read to `max_inline_lines`. Measured on
    DeepSWE (cattrs, haiku): the model asked `offset=729 limit=10` on a 60 KB
    file and got 239 lines back — the guard meant to bound reads had widened
    every targeted one into a 10 KB page. A cap narrows; it never widens."""
    big = tmp_path / "converters.py"
    big.write_text("\n".join(f"line {i}" for i in range(4000)), encoding="utf-8")
    assert big.stat().st_size > 16384

    d = _classify("Read", {"file_path": str(big), "offset": 729, "limit": 10}, tmp_path)
    assert d["rewrite"]["updatedInput"]["limit"] == 10  # caller asked for less: kept
    assert d["rewrite"]["updatedInput"]["offset"] == 729

    d = _classify("Read", {"file_path": str(big), "offset": 729, "limit": 5000}, tmp_path)
    assert d["rewrite"]["updatedInput"]["limit"] == 240  # over the cap: capped

    d = _classify("Read", {"file_path": str(big)}, tmp_path)
    assert d["rewrite"]["updatedInput"]["limit"] == 240  # no limit asked: the cap


# --------------------------------------------- whole-file read → outline
def _big_module(ws, name="converters.py", classes=6, methods=8):
    src = ["import os", "from typing import Any", ""]
    for c in range(classes):
        src.append(f"class Converter{c}:")
        for m in range(methods):
            src.append(f"    def method_{c}_{m}(self, value: Any) -> Any:")
            src += [f"        x = {i}  # padding line" for i in range(12)]
            src.append("        return value")
        src.append("")
    (ws / "src").mkdir(exist_ok=True)
    f = ws / "src" / name
    f.write_text("\n".join(src) + "\n", encoding="utf-8")
    assert f.stat().st_size > 16384
    return f


def test_whole_file_read_of_large_code_file_answers_with_its_outline(state_home, tmp_path):
    """Measured on DeepSWE (cattrs, haiku): a whole-file Read of a 64 KB module
    came back as its first 240 lines (~2.5k tok, 14% of the file, no map) and
    the session paged on. The skeleton is ~1k tok for every symbol with its
    line range, and makes the next read targeted."""
    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    f = _big_module(tmp_path)
    d = _classify("Read", {"file_path": str(f)}, tmp_path)
    assert d["decision"] == "deny" and "rewrite" not in d
    assert "[ctx skeleton repo:src/converters.py]" in d["reason"]
    assert "class Converter0" in d["reason"] and "method_5_7" in d["reason"]
    assert "offset and limit" in d["reason"] and "--symbol" in d["reason"]
    assert len(d["reason"]) < 6000  # a map, not a page


def test_slice_reads_and_non_code_files_keep_the_bounded_page(state_home, tmp_path):
    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    f = _big_module(tmp_path)
    d = _classify("Read", {"file_path": str(f), "offset": 40, "limit": 20}, tmp_path)
    assert d["rewrite"]["updatedInput"]["limit"] == 20  # a slice is never outlined
    assert "skeleton" not in d.get("reason", "")
    notes = tmp_path / "notes.txt"
    notes.write_text("x" * 20000, encoding="utf-8")
    d = _classify("Read", {"file_path": str(notes)}, tmp_path)
    assert d["rewrite"]["updatedInput"]["limit"] == 240  # no parser: first page as before


# ------------------------------------------- text readers and git listings
# Measured on Antigravity (182 labelled commands replayed through the hook):
# standalone `nl`/`seq`/`sort`/`cut`/`base64` were "unknown" and asked the
# human (a headless run ends with no answer), and `git ls-files`, `git
# ls-tree -r`, `git rev-list`, `git grep -n` and `ls src/*/*` ran natively and
# flooded the context.
TEXT_READERS = ["nl README.md", "fold -w 100 app.min.js", "fmt notes.txt",
                "column -t data.tsv", "paste a b", "comm a b", "seq 1 50000",
                "sort data.csv", "cut -d, -f2 data.csv", "uniq data.txt", "uniq -c -",
                "base64 icon.png", "rev a.txt", "expand a.txt", "unexpand a.txt"]
GIT_FLOODS = ["git ls-files", "git ls-files src", "git ls-tree -r HEAD", "git ls-tree -rt HEAD",
              "git rev-list HEAD", "git rev-list --all", "git rev-list -n 5000 HEAD",
              "git rev-list -n 5 --objects HEAD", "git grep -n 'def '", "git grep -c TODO",
              "git grep -l TODO", "git for-each-ref", "git for-each-ref --count=100000",
              "git ls-remote origin", "git show-ref", "git -C sub ls-files"]
GIT_BOUNDED = ["git rev-list --count HEAD", "git rev-list -n 5 HEAD",
               "git rev-list --max-count=3 HEAD", "git rev-list -1 HEAD", "git grep -q TODO",
               "git grep --quiet TODO", "git for-each-ref --count=5",
               "git show-ref --verify refs/heads/main", "git show-ref -q refs/heads/main",
               "git ls-tree HEAD", "git ls-tree HEAD src/"]
WRITERS = ["sort -o out.txt data.csv", "sort --output=out.txt data.csv", "sort -uo out.txt d",
           "uniq in.txt out.txt", "uniq -f 1 in.txt out.txt", "base64 -o out.b64 icon.png",
           "base64 --output=x icon.png"]


@pytest.mark.parametrize("cmd", TEXT_READERS + GIT_FLOODS + ["ls src/*/*", "ls -l *.py", "ls x?"])
def test_text_readers_git_listings_and_globbed_ls_are_captured(tmp_path, cmd):
    d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
    # the decision a host without input substitution gets is the deny; the
    # rewrite is what Antigravity and steering hosts run instead
    assert d["rewrite"]["updatedInput"]["CommandLine"].startswith("ctx run"), (cmd, d)
    assert d["rewrite"]["command"].endswith(shlex.quote(cmd)), d


@pytest.mark.parametrize("cmd", GIT_BOUNDED + ["ls", "ls -la src", "ls src/a.py"])
def test_bounded_listings_stay_native(tmp_path, cmd):
    d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
    assert d == {"decision": "allow"}, (cmd, d)


@pytest.mark.parametrize("cmd", WRITERS)
def test_text_readers_that_write_a_file_are_judged_as_unknown(tmp_path, cmd):
    d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
    assert d["decision"] == "force_ask" and "unknown output bound" in d["reason"], (cmd, d)
    assert "rewrite" not in d
    (tmp_path / "ctx.toml").write_text('version = 1\n[guard]\nunknown_command = "deny"\n')
    assert _classify("run_command", {"CommandLine": cmd}, tmp_path)["decision"] == "deny"


@pytest.mark.parametrize("cmd", ["yes", "tee out.txt", "tr a b"])
def test_yes_tee_and_tr_stay_unknown(tmp_path, cmd):
    d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
    assert d["decision"] == "force_ask" and "unknown output bound" in d["reason"], (cmd, d)


@pytest.mark.parametrize("cmd,want", [
    # (the verdict each pipeline had before these programs were captured alone)
    ("git ls-files | wc -l", {"decision": "allow"}),
    ("git grep -n TODO | head -5", {"decision": "allow"}),
    ("ls src/* | head", {"decision": "allow"}),
    ("seq 10 | sort", "capture"),
    ("cat data.csv | sort | uniq -c", "capture"),
    ("base64 icon.png | head -3", "capture"),   # vouched like the other text readers
    ("base64 -o x icon.png | head -3", "ask"),  # ... unless it writes a file
    ("./base64 icon.png | head -3", "ask"),     # ... or is a workspace script
    ("X=1 base64 icon.png | head -3", "ask"),   # ... or carries an environment prefix
    ("yes | head -n 5", "ask"),
    ("cat in | sort -o out.txt", "capture"),
])
def test_pipelines_keep_their_verdicts(tmp_path, cmd, want):
    d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
    if want == "capture":
        assert d["rewrite"]["updatedInput"]["CommandLine"].startswith("ctx run"), d
        assert "--shell" in d["rewrite"]["updatedInput"]["CommandLine"], d
    elif want == "ask":
        assert d["decision"] == "force_ask" and "rewrite" not in d, d
    else:
        assert d == want, d


def test_text_reader_keeps_every_existing_protection(tmp_path):
    (tmp_path / "ctx.toml").write_text('version = 1\n[guard]\ndeny_commands = ["sort"]\n')
    d = _classify("run_command", {"CommandLine": "sort a.txt"}, tmp_path)
    assert d["decision"] == "deny" and "rewrite" not in d
    (tmp_path / "ctx.toml").write_text("version = 1\n")
    d = _classify("run_command", {"CommandLine": f"nl {tmp_path}/.env"}, tmp_path)
    assert d["decision"] == "force_ask" and "secret" in d["reason"].lower()
    _steering_deny(tmp_path)
    for cmd in ("seq 1 5000", "git ls-files", "ls src/*/*"):
        d = _classify("run_command", {"CommandLine": cmd}, tmp_path)
        assert d["decision"] == "deny" and "rewrite" not in d, cmd


# ------------------------------------- review round 1: what a reader may not do
@pytest.mark.parametrize("cmd", [
    "sort --out=o.txt f", "sort --o=o.txt f", "sort --ou=o f", "base64 --out=x f",
    "sort -S 1k --compress-program=./ev.sh big", "sort --compress=./ev.sh big",
    "sort --co=./ev.sh big", "sort --files0-from=list", "sort --fi=list",
    "./sort f", "subdir/cut f", "LD_PRELOAD=./x.so sort f", "env LC_ALL=C sort f",
    "cut -c1 /proc/1/environ", "base64 /dev/urandom", "fold -w1 /dev/zero", "sort /dev/zero",
    "seq inf", "seq 1 1000000000", "seq -f %g 5", "seq 0 0 5", "seq a b",
])
def test_readers_that_write_run_or_read_outside_are_unknown(tmp_path, cmd):
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(tmp_path)}, tmp_path)
    assert d["decision"] == "force_ask" and "unknown output bound" in d["reason"], (cmd, d)
    assert "rewrite" not in d


@pytest.mark.parametrize("cmd", ["sort ~/.netrc", "sort ../../x", "cut -f1 /etc/passwd",
                                 "nl ..", "cut -d / -f1 f"])
def test_reader_operands_outside_the_workspace_are_unknown(tmp_path, cmd):
    ws = tmp_path / "ws"
    ws.mkdir()
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(ws)}, ws)
    assert d["decision"] == "force_ask" and "rewrite" not in d, (cmd, d)


@pytest.mark.parametrize("cmd", ["sort data/x.csv", "nl ./README.md", "cut -f1 sub/../a.txt"])
def test_reader_operands_inside_the_workspace_are_captured(tmp_path, cmd):
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(tmp_path)}, tmp_path)
    assert "rewrite" in d, (cmd, d)


@pytest.mark.parametrize("cmd,want", [
    ("seq 10", "allow"), ("seq 2 2 800", "allow"), ("seq -w 5 -1 1", "allow"),
    ("seq 1 401", "capture"), ("seq 1 1000000", "capture"), ("seq 1000001", "unknown"),
])
def test_seq_is_sized_by_its_arguments(tmp_path, cmd, want):
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(tmp_path)}, tmp_path)
    if want == "allow":
        assert d == {"decision": "allow"}, d
    elif want == "capture":
        assert "rewrite" in d, d
    else:
        assert d["decision"] == "force_ask" and "rewrite" not in d, d


@pytest.mark.parametrize("cmd", ["git for-each-ref --count=0", "git rev-list -n 0 HEAD",
                                 "git rev-list --max-count=0 HEAD", "git grep -e -q PAT",
                                 "git grep -f pats -q", "git grep --regexp=x -q"])
def test_git_counts_of_zero_and_q_as_a_pattern_are_not_bounds(tmp_path, cmd):
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(tmp_path)}, tmp_path)
    assert "rewrite" in d, (cmd, d)


# ---------------- review round 2: capture only what the classifier can see
def _ws_with_links(tmp_path):
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "sales.csv").write_text("a,1\nb,2\n")
    (ws / "lnk").symlink_to("/etc/passwd")
    (ws / "inlnk").symlink_to("data/sales.csv")
    (ws / ".env").write_text("K=v\n")
    (ws / "envlnk").symlink_to(ws / ".env")
    return ws


@pytest.mark.parametrize("cmd", [
    "sort lnk", "sort *.env", "sort data/*.csv", "nl data/sales.cs?", "sort ~user/x",
    "sort ~root/x", "sort -T/etc data/sales.csv",
    "sort -T /tmp data/sales.csv", "sort --temporary-directory=/tmp data/sales.csv",
    "sort --te=/tmp data/sales.csv", "seq -s xx 1 400", "seq --separator=, 1 5", "seq -x 5",
])
def test_reader_inputs_it_cannot_see_are_unknown(tmp_path, cmd):
    ws = _ws_with_links(tmp_path)
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(ws)}, ws)
    assert d["decision"] == "force_ask" and "rewrite" not in d, (cmd, d)


def test_reader_through_a_link_to_a_secret_gets_the_secret_verdict(tmp_path):
    ws = _ws_with_links(tmp_path)
    d = _classify("run_command", {"CommandLine": "sort envlnk", "Cwd": str(ws)}, ws)
    assert d["decision"] == "force_ask" and "secret" in d["reason"].lower()


@pytest.mark.parametrize("cmd", [
    "sort inlnk", "cut -d ',' -f2 data/sales.csv", "sort -t ',' -k2 data/sales.csv",
    "cut -d' ' -f1 data/sales.csv", "cut -d '*' -f1 data/sales.csv", "nl 'data/sales.csv'",
])
def test_quoted_literals_and_inside_links_are_still_captured(tmp_path, cmd):
    ws = _ws_with_links(tmp_path)
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(ws)}, ws)
    assert "rewrite" in d, (cmd, d)


def test_seq_with_only_equal_width_is_still_sized(tmp_path):
    assert _classify("run_command", {"CommandLine": "seq -w 1 10"}, tmp_path) == {
        "decision": "allow"}


#: Verdicts recorded on main (64e52b6) by running its classify() through a
#: workspace for each command: text readers inside compound or meta-bearing
#: commands are decided by the compound/meta rules, and the standalone rule
#: must not change any of them. The one deliberate change is `base64` as a
#: plain pipeline part, vouched like the other text readers (review round 1,
#: item 11): main asked, this tree captures.
MAIN_META_VERDICTS = {
    "sort < data/sales.csv": "captured", "sort < /etc/passwd": "captured",
    "sort data/sales.csv > out.txt": "captured", "sort data/sales.csv > out.txt 2>&1": "allow",
    "sort $HOME/x": "captured", "sort {a,/etc/passwd}": "captured",
    "sort $(echo data/sales.csv)": "captured", "sort `echo data/sales.csv`": "captured",
    "nl data/sales.csv && echo ok": "captured", "nl data/sales.csv; echo ok": "captured",
    "cat data/sales.csv | sort": "captured", "sort data/sales.csv | head -3": "captured",
    "cut -d, -f1 data/sales.csv | uniq -c": "captured", "seq 1 5 | paste -sd+": "captured",
    "sort -u data/sales.csv | wc -l": "captured", 'fold -w 5 "$F"': "captured",
    "comm <(sort a) <(sort b)": "captured", "expand data/sales.csv | rev": "captured",
}
DELIBERATE = {"base64 data/sales.csv | head -3": ("force_ask", "captured"),
              "echo x | base64": ("force_ask", "captured")}


@pytest.mark.parametrize("cmd", list(MAIN_META_VERDICTS) + list(DELIBERATE))
def test_text_readers_with_shell_metacharacters_keep_mains_verdict(tmp_path, cmd):
    ws = _ws_with_links(tmp_path)
    d = _classify("run_command", {"CommandLine": cmd, "Cwd": str(ws)}, ws)
    got = "captured" if "rewrite" in d else d["decision"]
    want = MAIN_META_VERDICTS.get(cmd) or DELIBERATE[cmd][1]
    assert got == want, (cmd, d)
