"""Every command ctx prints for the model to run next is approved by the hook
on Claude Code when it reads only the store by handle, and classified as a
path read otherwise -- tested at the source: real digests, pages and notes
are generated and every printed command is extracted, nothing substituted.

A printed command containing a placeholder (``'<pattern>'``, ``<i>``) is
not runnable as printed; those are exempt and listed by the test.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import struct
import sys
import zlib

import pytest

from ctx import hook

VERBS = "get|search|stats|diff|intake|view|q|map|def|refs"
_CMD_RE = re.compile(rf"(?<!\[)ctx (?:{VERBS})(?= )[^`\n]*")  # not a [ctx ...] header


def _cli(ws, argv, stdin_text=None):
    from ctx.cli import main

    out, err = io.StringIO(), io.StringIO()
    old_cwd, old_in = os.getcwd(), sys.stdin
    os.chdir(ws)
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main(argv)
            except SystemExit:
                pass
    finally:
        os.chdir(old_cwd)
        sys.stdin = old_in
    return out.getvalue() + err.getvalue()


def _png(w, h):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff" * (w * 3) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _printed(text):
    cmds = []
    for m in _CMD_RE.finditer(text):
        c = m.group(0).split("  ")[0].rstrip(" .,;)]")
        cmds.append(c)
    return cmds


@pytest.fixture()
def corpus(tmp_path, state_home, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ctx.toml").write_text("version = 1\n")
    (ws / ".git").mkdir()
    texts = []
    # command output through several profiles
    texts.append(_cli(ws, ["run", "--shell", "--", "seq 1 30000"]))
    texts.append(_cli(ws, ["run", "--shell", "--", "python3 -c \"import json;print(json.dumps([{'a':i,'b':'x'*20} for i in range(4000)]))\""]))
    texts.append(_cli(ws, ["run", "--shell", "--", "for i in $(seq 1 3000); do echo \"$i,name$i,$((i*7)),ok\"; done"]))
    texts.append(_cli(ws, ["run", "--shell", "--", "for i in $(seq 1 400); do echo \"FAILED tests/t$i.py::test_x - AssertionError\"; done; exit 1"]))
    # an intake paste with a chunk map, and its second page
    paste = "".join(f"section {i}\n" + ("detail line about thing %d\n" % i) * 40 for i in range(300))
    (ws / "paste.txt").write_text(paste)
    first = _cli(ws, ["intake", "paste.txt", "--label", "paste"])
    assert "ctx intake --chunks" in first, first[-500:]
    texts.append(first)
    for c in _printed(first):
        if c.startswith("ctx intake --chunks"):
            texts.append(_cli(ws, c.split()[1:]))
    # continuation chain and a span, followed exactly as printed
    for t in list(texts):
        for c in _printed(t):
            if c.startswith("ctx get ") and "<" not in c:
                texts.append(_cli(ws, c.split()[1:]))
                break
    # an image note (the media advise path)
    (ws / "shots").mkdir()
    (ws / "shots" / "screen.png").write_bytes(_png(2560, 1440))
    payload = {"tool_name": "Read", "cwd": str(ws), "session_id": "p",
               "tool_input": {"file_path": str(ws / "shots" / "screen.png")}}
    old = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use("claude-code")
        texts.append(json.loads(sys.stdout.getvalue())["hookSpecificOutput"]["additionalContext"])
    finally:
        sys.stdin, sys.stdout = old
    return ws, home, texts


def test_every_printed_command_is_approved_or_exempt(corpus):
    ws, home, texts = corpus
    cmds = sorted({c for t in texts for c in _printed(t)})
    assert len(cmds) >= 8, cmds
    exempt, shapes = [], set()
    for c in cmds:
        if "<" in c or ">" in c or "…" in c:
            exempt.append(c)
            continue
        payload = {"tool_name": "Bash", "cwd": str(ws), "tool_input": {"command": c}}
        assert hook._ctx_self_approved(payload, str(ws)), c
        argv = hook._ctx_words(c)
        shapes.add((argv[1], hook._printed_read_kind(argv[1], argv[2:])))
    # The shapes the corpus must have produced.
    assert ("get", "store") in shapes and ("intake", "store") in shapes, shapes
    # The image note names a `ctx view` crop only when Pillow can make one;
    # without it the note says so and prints no command to approve.
    if importlib.util.find_spec("PIL") is not None:
        assert ("view", "path") in shapes, shapes
    print("approved shapes:", sorted(shapes))
    print("exempt (placeholders):", exempt)


def test_printed_path_reads_withdraw_under_a_read_rule(corpus):
    ws, home, texts = corpus
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Read(./secrets/**)"]}}))
    for c in sorted({c for t in texts for c in _printed(t)}):
        if "<" in c or ">" in c or "…" in c:
            continue
        argv = hook._ctx_words(c)
        kind = hook._printed_read_kind(argv[1], argv[2:])
        payload = {"tool_name": "Bash", "cwd": str(ws), "tool_input": {"command": c}}
        assert hook._ctx_self_approved(payload, str(ws)) is (kind == "store"), (c, kind)


@pytest.mark.parametrize("command", [
    "ctx get run:0660dede8dbe #stdout",               # '#' starting a comment
    "ctx get run:0660dede8dbe#stdout # trailing",
    "ctx get run:0660dede8dbe#stdout#stderr",
    "ctx get run:0660dede8dbe#stdout>out.txt",         # redirection
    "ctx get run:0660dede8dbe#stdout >out.txt",
    "X=1 ctx get run:0660dede8dbe#stdout",             # assignment prefix
    "env X=1 ctx get run:0660dede8dbe#stdout",
    "ctx get run:0660dede8dbe#stdout; rm -rf x",       # a second command
    "ctx get run:0660dede8dbe#stdout && rm -rf x",
    "ctx get run:0660dede8dbe#stdout\nrm -rf x",
    "ctx get run:0660dede8dbe#$(id)",
    "ctx get run:0660dede8dbe#stdout --lines 1:40#x",
    "ctx get run:#stdout",
    "ctx get run:xyz#stdout",
    "ctx get ws:other/run:0660dede8dbe#stdout",
    "ctx intake notes.txt", "ctx intake -", "ctx intake --clip", "ctx intake --chunks repo:a.txt",
    "ctx get run:0660dede8dbe --snapcompact",
    "ctx stats --session",
    "./ctx get run:0660dede8dbe#stdout",
])
def test_malformed_hash_and_other_shapes_are_not_approved(tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}}
    assert hook._ctx_self_approved(payload, str(tmp_path)) is False, command


# ------------------------------------------- handle grammar, one definition
BAD_IDS = [
    "run:../../etc/passwd", "run:", "run:abc12", "run:" + "a" * 65, "run:%2e%2e/x",
    "run:abc123/x", "run:abc123\x00", "run:abc123#", "run:abc123#stdout/../x",
    "run:abc123#stdin", "run:abc123#stdout#stderr", "run:/etc/passwd", "run:abc12g",
    "blob:../../x", "snapshot:../x", "search:zz", "checkpoint:", "span:abc", "sha256:../x",
]


@pytest.mark.parametrize("bad", BAD_IDS)
@pytest.mark.parametrize("template", [
    "ctx get {h}", "ctx get {h} --lines 1:40", "ctx search {h} root", "ctx stats {h}",
    "ctx view {h}", "ctx diff {h} run:0660dede8dbe", "ctx intake --chunks {h}",
])
def test_malformed_handles_are_never_approved(tmp_path, monkeypatch, bad, template):
    monkeypatch.setenv("HOME", str(tmp_path))
    command = template.format(h=bad)
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}}
    assert hook._ctx_self_approved(payload, str(tmp_path)) is False, command


@pytest.mark.parametrize("span", ["abc", "ABCDEF1234", "0123456789a", "../x", "0123456789;"])
def test_malformed_span_ids_are_never_approved(tmp_path, monkeypatch, span):
    monkeypatch.setenv("HOME", str(tmp_path))
    payload = {"tool_name": "Bash", "cwd": str(tmp_path),
               "tool_input": {"command": f"ctx get run:0660dede8dbe#stdout --span {span}"}}
    assert hook._ctx_self_approved(payload, str(tmp_path)) is False


def test_hook_and_commands_share_one_handle_grammar():
    from ctx import refgrammar, refs

    assert refs._HEX_RE is refgrammar.HEX_RE
    for good in ("run:0660dede8dbe", "run:0660DEDE8dbe#stderr", "blob:0660dede8dbe"):
        refs.parse_ref(good)
        assert refgrammar.ANY_HANDLE_RE.fullmatch(good)
    for bad in BAD_IDS:
        assert not refgrammar.ANY_HANDLE_RE.fullmatch(bad), bad


def test_view_validates_run_ids_like_every_other_verb(tmp_path, state_home):
    (tmp_path / "ctx.toml").write_text("version = 1\n")
    out = _cli(tmp_path, ["view", "run:../../etc/passwd"])
    assert "need 6-64 hex characters" in out or "invalid run id" in out, out


def test_a_span_is_honoured_only_on_its_own_artifact(tmp_path, state_home):
    from ctx._retrieval.common import RetrievalError
    from ctx.retrieval import Selector, get
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n")
    _cli(tmp_path, ["run", "--shell", "--", "seq 1 50"])
    _cli(tmp_path, ["run", "--shell", "--", "seq 100 150"])
    ws = resolve_workspace(str(tmp_path))
    store = Store(ws.workspace_id)
    other = store.put_blob(b"SNAPSHOT-OF-SOMETHING-ELSE\n" * 5)
    sid = store.register_span(other, "region", a=1, b=2)
    rows = store.db.execute("SELECT id FROM objects WHERE kind='run'").fetchall()
    short = rows[0][0][:12]
    with pytest.raises(RetrievalError, match="does not belong"):
        get(store, ws, f"run:{short}#stdout", Selector(span=sid))
    assert "SNAPSHOT-OF" in get(store, ws, f"blob:{other[:12]}", Selector(span=sid))


# ----------------------------------------------- the path side, by position
PATH_FLAG_FORMS = {
    "get": ["", "--lines 1:40", "--lines=1:40", "--bytes 1:64", "--records 1:2",
            "--json-pointer /a/0", "--json-pointer '/a#b'", "--symbol mod.Cls.fn", "--hashlines",
            "--lines 1:40 --hashlines", "--lines 10:20@abc123"],
    "search": ["needle", "needle --context 3", "needle --glob 'src/*.py'", "needle --scope api",
               "needle --max-matches 5 --fixed --all", "'a.b' --context=2"],
    "stats": ["", "--scope api"],
    "view": ["", "--grid 3x4 --cell B2", "--region 0,0,10,10", "--at 1:30", "--range 2-9 --every 0.5",
             "--fit 800 --count 3", "--sheet", "--json"],
}


def _path_cases():
    for verb, forms in PATH_FLAG_FORMS.items():
        target = "shot.png" if verb == "view" else "big.txt"
        for spelling in (target, f"repo:{target}", f"./{target}"):
            if verb == "view" and spelling.startswith("repo:"):
                continue  # view takes a file path or a store address, not repo:
            for form in forms:
                if verb == "search":
                    yield f"ctx search {spelling} {form}".strip()
                else:
                    yield f"ctx {verb} {spelling} {form}".strip()
    for c in ("ctx refs main", "ctx map", "ctx def main", "ctx stats"):
        yield c


PATH_CASES = sorted(set(_path_cases()))


def test_path_side_matrix_is_wide():
    assert len(PATH_CASES) >= 75, len(PATH_CASES)


@pytest.mark.parametrize("command", PATH_CASES)
def test_path_reads_by_position(tmp_path, monkeypatch, command):
    """A flag's value is never a target: approved with no Read rule visible,
    no decision once one is."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text("x\n")
    (ws / "shot.png").write_bytes(b"x")
    payload = {"tool_name": "Bash", "cwd": str(ws), "tool_input": {"command": command}}
    argv = hook._ctx_words(command)
    assert argv is not None, command
    assert hook._printed_read_kind(argv[1], argv[2:]) == "path", command
    assert hook._ctx_self_approved(payload, str(ws)) is True, command
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Read(./secrets/**)"]}}))
    assert hook._ctx_self_approved(payload, str(ws)) is False, command


@pytest.mark.parametrize("command", [
    "ctx get ../big.txt --lines 1:40", "ctx get /etc/passwd --lines 1:40",
    "ctx get repo:../x --lines 1:40", "ctx search needle --glob '../*'",
    "ctx get big.txt --lines 1:40 extra", "ctx view /etc/hostname --grid 3x4",
])
def test_path_targets_still_cannot_leave(tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}}
    assert hook._ctx_self_approved(payload, str(tmp_path)) is False, command


def _hook_text(ws, tool, tool_input, steering=None):
    if steering:
        (ws / "ctx.toml").write_text(f'version = 1\n[guard]\nsteering = "{steering}"\n')
    payload = {"tool_name": tool, "cwd": str(ws), "session_id": "pp", "tool_input": tool_input}
    old = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use("claude-code")
        hso = json.loads(sys.stdout.getvalue()).get("hookSpecificOutput", {})
    finally:
        sys.stdin, sys.stdout = old
    return " ".join(str(hso.get(k, "")) for k in ("additionalContext", "permissionDecisionReason"))


def test_commands_printed_by_the_read_guard_and_steering_are_approved(tmp_path, state_home,
                                                                     monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ctx.toml").write_text("version = 1\n")
    (ws / ".git").mkdir()
    (ws / "big.log").write_text("".join(f"line {i} payload payload\n" for i in range(60000)))
    (ws / "shots").mkdir()
    (ws / "shots" / "screen.png").write_bytes(_png(2560, 1440))
    texts = [
        _hook_text(ws, "Read", {"file_path": str(ws / "big.log")}),                  # narrowed read note
        _hook_text(ws, "Read", {"file_path": str(ws / "shots" / "screen.png")}),     # media note
        _hook_text(ws, "Grep", {"pattern": "payload", "path": "."}),                 # search steering
        _hook_text(ws, "Glob", {"pattern": "**/*.log"}),
        _hook_text(ws, "Read", {"file_path": str(ws / "big.log")}, steering="deny"),  # deny advice
    ]
    cmds = sorted({c for t in texts for c in _printed(t)})
    runnable = [c for c in cmds if not any(x in c for x in ("<", ">", "…"))]
    assert runnable, texts
    for c in runnable:
        payload = {"tool_name": "Bash", "cwd": str(ws), "tool_input": {"command": c}}
        assert hook._ctx_self_approved(payload, str(ws)), c
    print("printed by the read guard / steering:", runnable)
