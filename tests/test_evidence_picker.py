"""The optional decision-model picker for the evidence map, against a local
stub server (never the real service), plus the failure-hook path and the
default mode's no-network guarantee."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import make_store, make_ws
from ctx.digest import digest_output

KEY_ENV = "CTX_TEST_PICKER_KEY"
KEY = "picker-test-key-VALUE-9f8e7d6c5b4a"


class _Stub:
    def __init__(self):
        self.behaviour = "ok"
        self.requests: list[dict] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                doc = json.loads(body)
                stub.requests.append({"key": self.headers.get("x-goog-api-key"), "doc": doc})
                if stub.behaviour == "slow":
                    time.sleep(1.5)
                if stub.behaviour == "500":
                    self.send_response(500)
                    self.end_headers()
                    return
                if stub.behaviour == "garbage":
                    out = b"{not json"
                elif stub.behaviour == "missing":
                    out = json.dumps({"answers": {}}).encode()
                else:
                    answers = {qid: {"type": "noul",
                                     "noul": 0.97 if "rootcause" in q["instructions"] else 0.03}
                               for qid, q in doc["questions"].items()}
                    out = json.dumps({"answers": answers, "usage": {"total_tokens": 321}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(out)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1beta/decisions"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def stub():
    s = _Stub()
    yield s
    s.close()


def _log() -> str:
    rows = [f"2025-01-01 10:{i // 60 % 60:02d}:{i % 60:02d} INFO step {i} ok " + "p" * 30
            for i in range(600)]
    rows[100] = "2025-01-01 10:01:40 FATAL alpha crashed"
    rows[250] = "2025-01-01 10:04:10 FATAL beta crashed"
    rows[400] = "2025-01-01 10:06:40 FATAL gamma crashed"
    rows[500] = "2025-01-01 10:08:20 ERROR rootcause marker: disk is read-only"
    rows[550] = "2025-01-01 10:09:10 ERROR rootcause marker: disk is read-only"
    return "\n".join(rows) + "\n"


def _config(workspace_dir: Path, mode: str, url: str = "", timeout_ms: int = 3000) -> None:
    text = "version = 1\n"
    if mode:
        text += (f'[picker]\nmode = "{mode}"\nendpoint = "{url}"\n'
                 f'api_key_env = "{KEY_ENV}"\ntimeout_ms = {timeout_ms}\n')
    (workspace_dir / "ctx.toml").write_text(text, encoding="utf-8")


def _run(workspace_dir, picker=True):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    return digest_output(store, ws, "Bash", _log(), argv=["./ci.sh"], picker=picker)


def _ledger(workspace_dir) -> list[dict]:
    p = workspace_dir / ".ctx-session-reads" / "picker.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _failures(workspace_dir) -> list[dict]:
    p = workspace_dir / ".ctx-session-reads" / "guard-failures.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _all_text_under(*roots: Path) -> str:
    out = []
    for root in roots:
        for p in Path(root).rglob("*"):
            if p.is_file():
                out.append(p.read_bytes().decode("utf-8", "replace"))
    return "\n".join(out)


def test_local_is_the_default_and_sends_nothing(state_home, workspace_dir, stub, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    _config(workspace_dir, "")
    digest, _ = _run(workspace_dir)
    assert "evidence map (exact)" in digest
    assert stub.requests == []
    assert _ledger(workspace_dir) == []
    assert "picked by" not in digest


def test_model_mode_expands_the_questions_pick_and_keeps_the_list(state_home, workspace_dir,
                                                                   stub, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    _config(workspace_dir, "")
    local, local_id = _run(workspace_dir)
    assert "rootcause" in local and "    L501: " not in local  # listed, not expanded locally
    _config(workspace_dir, "model", stub.url)
    model, model_id = _run(workspace_dir)
    assert len(stub.requests) == 1
    req = stub.requests[0]
    assert req["key"] == KEY                                    # sent only in the header
    assert KEY not in json.dumps(req["doc"])
    assert req["doc"]["model"] == "gemini-decision-1"
    assert all(q["type"] == "noul" for q in req["doc"]["questions"].values())
    assert "./ci.sh" in req["doc"]["input"]
    assert "L501: 2025-01-01 10:08:20 ERROR rootcause marker" in model
    assert "picked by the question (p=0.97)" in model
    assert "picked by the local rule" in model
    # the accounting is the same: the model chooses, it cannot hide
    def account(d):
        line = next(ln for ln in d.splitlines() if ln.startswith("evidence map"))
        return line.split(" · ")
    assert account(model)[:2] == account(local)[:2]
    shown, listed, unlisted = (int(x.split()[0]) for x in account(model)[2:])
    assert shown + listed + unlisted == int(account(model)[1].split()[0])
    assert model_id != local_id
    rec = _ledger(workspace_dir)[-1]
    assert rec["mode"] == "model" and rec["handle"] == f"run:{model_id}"
    assert rec["expanded"][0] == "stdout:L501"
    assert rec["tokens"] == 321 and rec["error"] is None
    assert rec["model_order"][0] == "stdout:L501" and rec["local_order"][0] != "stdout:L501"
    ws = make_ws(workspace_dir)
    manifest = make_store(ws).get_manifest(model_id)
    assert manifest["digest"]["picker"].startswith("model:")


def test_shadow_delivers_the_local_digest_and_records_both(state_home, workspace_dir, stub,
                                                           monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    _config(workspace_dir, "")
    local, local_id = _run(workspace_dir)
    _config(workspace_dir, "shadow", stub.url)
    shadow, shadow_id = _run(workspace_dir)
    assert len(stub.requests) == 1
    assert shadow == local and shadow_id == local_id
    rec = _ledger(workspace_dir)[-1]
    assert set(rec) >= {"schema", "ts", "mode", "handle", "candidates", "messages", "local_order",
                        "scores", "model_order", "local_expanded", "expanded", "latency_ms",
                        "tokens", "error", "model"}
    assert rec["schema"] == "ctx.picker/v1" and rec["mode"] == "shadow"
    assert rec["handle"] == f"run:{shadow_id}"
    assert rec["expanded"] == rec["local_expanded"]
    assert rec["model_order"][0] == "stdout:L501"
    assert len(rec["scores"]) == rec["candidates"] == len(rec["local_order"])
    assert isinstance(rec["latency_ms"], float)


@pytest.mark.parametrize("behaviour,error", [
    ("slow", "timeout"), ("500", "http 500"), ("garbage", "malformed answer"),
    ("missing", "malformed answer"), ("nokey", "missing key"),
])
def test_fail_open_is_exactly_the_local_digest(state_home, workspace_dir, stub, monkeypatch,
                                               behaviour, error):
    if behaviour == "nokey":
        monkeypatch.delenv(KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(KEY_ENV, KEY)
    _config(workspace_dir, "")
    local, local_id = _run(workspace_dir)
    stub.behaviour = behaviour
    _config(workspace_dir, "model", stub.url, timeout_ms=300)
    t0 = time.perf_counter()
    got, got_id = _run(workspace_dir)
    assert time.perf_counter() - t0 < 1.4                     # the deadline held
    assert got == local and got_id == local_id
    rec = _ledger(workspace_dir)[-1]
    assert rec["error"] == error and rec["expanded"] == rec["local_expanded"]
    fails = [f for f in _failures(workspace_dir) if f["op"] == "picker"]
    assert len(fails) == 1 and fails[0]["detail"] == error
    if behaviour == "nokey":
        assert stub.requests == []


def test_key_is_never_written_or_shown(state_home, workspace_dir, stub, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    for mode, behaviour in (("model", "ok"), ("shadow", "ok"), ("model", "500"), ("model", "slow")):
        stub.behaviour = behaviour
        _config(workspace_dir, mode, stub.url, timeout_ms=300)
        digest, _ = _run(workspace_dir)
        assert KEY not in digest
    assert KEY not in _all_text_under(workspace_dir, state_home)


def test_only_live_paths_consult_the_picker(state_home, workspace_dir, stub, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    _config(workspace_dir, "model", stub.url)
    _run(workspace_dir, picker=False)        # replay / re-digest callers
    assert stub.requests == []


def test_replay_reports_follow_up_fetches_per_mode(state_home, workspace_dir, stub, monkeypatch,
                                                   capsys):
    from ctx.cli import main
    from ctx.retrieval import Selector, get

    monkeypatch.setenv(KEY_ENV, KEY)
    _config(workspace_dir, "shadow", stub.url)
    _, rid1 = _run(workspace_dir)
    _config(workspace_dir, "model", stub.url)
    _, rid2 = _run(workspace_dir)
    ws = make_ws(workspace_dir)
    get(make_store(ws), ws, f"run:{rid2}#stdout", Selector(lines=(1, 3)))
    get(make_store(ws), ws, f"run:{rid2}#stdout", Selector(lines=(4, 6)))
    capsys.readouterr()
    main(["--workspace", str(workspace_dir), "replay", "--picker", "--json"])
    report = json.loads(capsys.readouterr().out)
    model, shadow = report["modes"]["model"], report["modes"]["shadow"]
    assert (model["digests"], model["followed_by_fetch"], model["fetches"]) == (1, 1, 2)
    assert (shadow["digests"], shadow["followed_by_fetch"]) == (1, 0)
    assert shadow["compared"] == 1 and shadow["top_pick_differs"] == 1
    main(["--workspace", str(workspace_dir), "replay", "--picker"])
    assert "followed-by-fetch" in capsys.readouterr().out


def test_retrieval_telemetry_records_the_run_handle(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    _config(workspace_dir, "")
    _, rid = _run(workspace_dir)
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    get(store, ws, f"run:{rid}#stdout", Selector(lines=(1, 2)))
    events = [json.loads(x) for x in (store.audit_dir / "telemetry.jsonl").read_text().splitlines()]
    assert events[-1]["op"] == "get" and events[-1]["ref"] == f"run:{rid}"


# ---------------------------------------------------------------- no network by default
def test_default_hook_imports_no_network_module(tmp_path):
    root = Path(__file__).resolve().parent.parent
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    payload = json.dumps({"session_id": "s", "cwd": str(ws), "hook_event_name": "PostToolUse",
                          "tool_name": "Bash", "tool_input": {"command": "./ci.sh"},
                          "tool_response": {"stdout": _log() * 20, "stderr": ""}})
    probe = ("','.join(m for m in ('ctx.digest.decision_client', 'ctx.digest.picker', "
             "'urllib.request', 'http.client', 'ssl') if m in sys.modules)")
    code = ("import sys\nfrom ctx.cli import main\n"
            "try:\n    main()\nexcept SystemExit:\n    pass\n"
            f"sys.stderr.write('\\nLOADED=' + {probe})\n")
    env = {"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin",
           "CTX_STATE_HOME": str(tmp_path / "state"), "HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code, "hook", "claude-code", "post-tool-use"],
                         input=payload, capture_output=True, text=True, env=env, cwd=str(ws),
                         timeout=120)
    emitted = json.loads(out.stdout)
    assert "evidence map (exact)" in json.dumps(emitted)        # the gate did digest it
    loaded = out.stderr.rsplit("LOADED=", 1)[1].strip()
    assert loaded == "", loaded


# ---------------------------------------------------------------- failure hook
def _failure_hook(payload):
    from ctx.hook import main_post_tool_use_failure

    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        assert main_post_tool_use_failure("claude-code") == 0
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def test_failure_hook_adds_the_evidence_map_for_a_large_failure(state_home, workspace_dir):
    big = "Exit code 1\n" + _log()
    out = _failure_hook({"cwd": str(workspace_dir), "session_id": "s1",
                         "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                         "tool_input": {"command": "./ci.sh"}, "error": big})
    ctx_text = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "evidence map (exact)" in ctx_text and "[ctx run:" in ctx_text
    assert "FATAL alpha crashed" in ctx_text
    small = _failure_hook({"cwd": str(workspace_dir), "session_id": "s1",
                           "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                           "tool_input": {"command": "false"}, "error": "Exit code 1\nboom"})
    assert small == {}


def test_failure_hook_leaves_a_ctx_run_digest_alone(state_home, workspace_dir):
    text = "[ctx run:abcdef123456 profile=text/v1]\n" + _log()
    out = _failure_hook({"cwd": str(workspace_dir), "session_id": "s1",
                         "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                         "tool_input": {"command": "ctx run -- ./ci.sh"}, "error": text})
    assert out == {}
