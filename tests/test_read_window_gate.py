"""A Read the guard itself bounded comes back as readable file text.

Measured (20 Claude Code sessions): PreToolUse limits a large-file Read to
240 lines; PostToolUse then measured the result on its JSON wrapper, found it
over the 16 KB output budget, and replaced it with a ``json/v1`` SHAPE digest
holding no line of the file. Six sessions lost their Read that way.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

import pytest

from ctx import hook


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    return d


def _run(stage: str, payload: dict) -> dict:
    fn = {"pre": hook.main_pre_tool_use, "post": hook.main_post_tool_use}[stage]
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        fn("claude-code")
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout


def _log(ws, width: int, n: int = 6000) -> tuple[str, list[str]]:
    lines = [f"2025-02-11 09:{i // 60 % 60:02d}:{i % 60:02d} INFO worker-{i % 7} batch {i} "
             .ljust(width, "x") for i in range(n)]
    path = ws / "server.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path), lines


def _read_result(path: str, lines: list[str], start: int = 1, n: int = 240) -> dict:
    """Claude Code's text-Read result, as PostToolUse receives it (2.1.322)."""
    return {"type": "text", "file": {"filePath": path,
                                     "content": "\n".join(lines[start - 1:start - 1 + n]),
                                     "numLines": n, "startLine": start,
                                     "totalLines": len(lines) + 1}}


def _bounded_read(ws, path: str, tr: dict, session: str = "s1") -> dict:
    pre = _run("pre", {"tool_name": "Read", "cwd": str(ws), "session_id": session,
                       "tool_input": {"file_path": path}})
    assert pre["hookSpecificOutput"]["updatedInput"]["limit"] == 240  # the guard bounded it
    return _run("post", {"tool_name": "Read", "cwd": str(ws), "session_id": session,
                         "tool_input": {"file_path": path, "limit": 240},
                         "tool_response": tr})


def _ledger_total(ws, session: str = "s1") -> int:
    return int((ws / ".ctx-session-reads" / f"{session}.count").read_text())


# ------------------------------------------------------------ normaliser
def test_normaliser_reads_the_text_read_shape():
    tr = {"type": "text", "file": {"filePath": "/w/a.log", "content": "one\ntwo",
                                   "numLines": 2, "startLine": 1, "totalLines": 2}}
    assert hook._normalize_tool_response(tr) == ("one\ntwo", "")


@pytest.mark.parametrize("tr,text", [
    ({"mode": "content", "numFiles": 0, "filenames": [], "content": "a.py:1:x\nb.py:2:y",
      "numLines": 2}, "a.py:1:x\nb.py:2:y"),
    ({"mode": "count", "numFiles": 1, "filenames": [], "content": "a.py:3", "numMatches": 3},
     "a.py:3"),
    ({"mode": "files_with_matches", "numFiles": 2, "filenames": ["a.py", "b.py"]}, "a.py\nb.py"),
    ({"filenames": ["a.py", "b.py"], "durationMs": 4, "numFiles": 2, "truncated": False},
     "a.py\nb.py"),
])
def test_normaliser_reads_grep_and_glob_listings(tr, text):
    assert hook._normalize_tool_response(tr) == (text, "")


@pytest.mark.parametrize("tr", [
    {"type": "image", "file": {"base64": "AAAA", "type": "image/png"}},
    {"type": "text", "file": {"content": "x", "secret_extra": 1}},
    {"filenames": ["a"], "unknownKey": "kept"},
])
def test_normaliser_keeps_unknown_shapes_lossless(tr):
    assert hook._normalize_tool_response(tr) == (json.dumps(tr, ensure_ascii=False,
                                                            sort_keys=True), "")


# ------------------------------------------------------- bounded windows
def test_bounded_window_under_budget_passes_untouched(ws):
    path, lines = _log(ws, 40)
    tr = _read_result(path, lines)
    assert len(tr["file"]["content"].encode()) < 16384 < len(json.dumps(tr)) * 2
    out = _bounded_read(ws, path, tr)
    assert "updatedToolOutput" not in out.get("hookSpecificOutput", {})
    # Charged what was delivered, not the up-front max_inline_bytes.
    assert _ledger_total(ws) == len(tr["file"]["content"].encode())


def test_window_just_under_budget_as_text_is_not_digested_on_its_json(ws):
    # 240 lines of 68 bytes: 16.3 KB of text, over 16 KB once JSON-wrapped.
    path, lines = _log(ws, 67)
    tr = _read_result(path, lines)
    assert len(tr["file"]["content"].encode()) <= 16384 < len(json.dumps(tr).encode())
    out = _bounded_read(ws, path, tr)
    assert "updatedToolOutput" not in out.get("hookSpecificOutput", {})


def test_bounded_window_over_budget_is_cut_to_whole_lines_with_a_note(ws):
    path, lines = _log(ws, 100)
    tr = _read_result(path, lines)
    out = _bounded_read(ws, path, tr)
    hso = out["hookSpecificOutput"]
    uto = hso["updatedToolOutput"]
    f = uto["file"]
    k = f["numLines"]
    assert 100 < k < 240
    assert uto["type"] == "text" and set(f) == set(tr["file"])
    assert f["content"] == "\n".join(lines[:k])  # exactly what Read limit=k returns
    assert f["startLine"] == 1 and f["totalLines"] == tr["file"]["totalLines"]
    note = hso["additionalContext"]
    assert f"lines 1-{k} of 6,001" in note and f"offset={k + 1}" in note
    assert "json/v1" not in json.dumps(out) and "shape (exact)" not in json.dumps(out)
    sent = len(json.dumps(uto, ensure_ascii=False).encode()) + len(note.encode())
    assert sent <= 16384
    assert _ledger_total(ws) == len(f["content"].encode())
    row = json.loads((ws / ".ctx-session-reads" / "coverage.jsonl").read_text()
                     .splitlines()[-1])
    assert row["decision"] == "window_fitted" and row["emitted_bytes"] <= 16384


def test_cut_window_keeps_its_start_line(ws):
    path, lines = _log(ws, 100)
    tr = _read_result(path, lines, start=241)
    out = _bounded_read(ws, path, tr)
    f = out["hookSpecificOutput"]["updatedToolOutput"]["file"]
    k = f["numLines"]
    assert f["content"] == "\n".join(lines[240:240 + k])
    assert f"lines 241-{240 + k}" in out["hookSpecificOutput"]["additionalContext"]
    assert f"offset={241 + k}" in out["hookSpecificOutput"]["additionalContext"]


def test_cut_never_shows_a_secret_prefix_at_its_edge(ws):
    path, lines = _log(ws, 100)
    tr = _read_result(path, lines)
    k = _bounded_read(ws, path, tr)["hookSpecificOutput"]["updatedToolOutput"]["file"]["numLines"]
    # A private key whose BEGIN line is the last line kept: the cut must not
    # show it as a prefix too short for the redaction pattern to match.
    lines2 = list(lines)
    lines2[k - 1] = "-----BEGIN RSA PRIVATE KEY-----"
    lines2[k] = "MIIEowIBAAKCAQEA" + "q" * 60
    lines2[k + 1] = "-----END RSA PRIVATE KEY-----"
    tr2 = _read_result(path, lines2)
    out = _run("post", {"tool_name": "Read", "cwd": str(ws), "session_id": "s2",
                        "tool_input": {"file_path": path, "limit": 240}, "tool_response": tr2})
    content = out["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"]
    assert "BEGIN RSA PRIVATE KEY" not in content


# ------------------------------------------------- still protected
def _handle(text: str) -> str:
    m = re.search(r"ctx get (run:[0-9a-f]+#stdout)", text)
    assert m, text
    return m.group(1)


def _ctx_get(ws, handle: str) -> str:
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "-m", "ctx", "get", handle], cwd=ws, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_unbounded_huge_single_line_read_is_still_digested(ws):
    # Not a window the guard chose (no such file on disk to have bounded):
    # the ordinary gate, now over the file's text instead of its JSON.
    blob = "k" * 200_000
    tr = {"type": "text", "file": {"filePath": str(ws / "gone.min.js"), "content": blob,
                                   "numLines": 1, "startLine": 1, "totalLines": 1}}
    out = _run("post", {"tool_name": "Read", "cwd": str(ws), "session_id": "s1",
                        "tool_input": {"file_path": str(ws / "gone.min.js")},
                        "tool_response": tr})
    content = out["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"]
    assert content.startswith("[ctx run:") and "json/v1" not in content
    assert len(content) < 16384
    got = _ctx_get(ws, _handle(content))
    assert "--lines 1:1 of 1\nL1: kkkk" in got


def test_bounded_window_of_one_enormous_line_falls_back_to_a_text_digest(ws):
    path = ws / "bundle.min.js"
    path.write_text("v" * 300_000, encoding="utf-8")
    tr = {"type": "text", "file": {"filePath": str(path), "content": "v" * 300_000,
                                   "numLines": 1, "startLine": 1, "totalLines": 1}}
    out = _bounded_read(ws, str(path), tr)
    content = out["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"]
    assert content.startswith("[ctx run:") and "json/v1" not in content
    assert len(content) < 16384


def test_stored_artifact_of_a_gated_read_is_file_text(ws):
    lines = [f"line {i} " + "z" * 80 for i in range(400)]
    tr = {"type": "text", "file": {"filePath": "/elsewhere/a.log", "content": "\n".join(lines),
                                   "numLines": 400, "startLine": 1, "totalLines": 400}}
    out = _run("post", {"tool_name": "Read", "cwd": str(ws), "session_id": "s1",
                        "tool_input": {"file_path": "/elsewhere/a.log"}, "tool_response": tr})
    handle = _handle(out["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"])
    got = _ctx_get(ws, handle)
    assert "of 400\nL1: line 0 zzzz" in got
    assert '"content"' not in got and "\\n" not in got
