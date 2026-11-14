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

KEY_ENV = "CTX_PICKER_API_KEY"   # one of the allowed key names
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
                if stub.behaviour.startswith("redirect:"):
                    self.send_response(307)
                    self.send_header("Location", stub.behaviour.split(":", 1)[1])
                    self.end_headers()
                    return
                if stub.behaviour == "garbage":
                    out = b"{not json"
                elif stub.behaviour == "missing":
                    out = json.dumps({"answers": {}}).encode()
                elif stub.behaviour == "range":
                    out = json.dumps({"answers": {qid: {"noul": 1.7} for qid in doc["questions"]}}).encode()
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


_MP: list = []


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No real user config or CTX_PICKER_* variable leaks into a test: the
    account home (the one place that can enable the picker) is a temporary
    directory, injected through ctx.config.account_home."""
    import ctx.config as cfg

    home = tmp_path / "account-home"
    home.mkdir()
    monkeypatch.setattr(cfg, "account_home", lambda: str(home))
    for name in ("MODE", "ENDPOINT", "MODEL", "API_KEY_ENV", "TIMEOUT_MS", "THRESHOLD",
                 "MAX_CANDIDATES", "MAX_CANDIDATE_BYTES"):
        monkeypatch.delenv(f"CTX_PICKER_{name}", raising=False)
    _MP[:] = [monkeypatch, home]
    yield
    _MP.clear()


def _user_file(body: str) -> Path:
    f = _MP[1] / ".config" / "ctx" / "config.toml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    f.chmod(0o600)
    return f


def _config(workspace_dir: Path, mode: str, url: str = "", timeout_ms: int = 3000) -> None:
    """Enable the picker the only way that counts: the account-home file;
    "" means the default (local)."""
    f = _MP[1] / ".config" / "ctx" / "config.toml"
    if not mode:
        if f.exists():
            f.unlink()
        return
    _user_file(f'[picker]\nmode = "{mode}"\nendpoint = "{url}"\n'
               f'api_key_env = "{KEY_ENV}"\ntimeout_ms = {timeout_ms}\n')


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
    recs = _ledger(workspace_dir)
    assert [r["mode"] for r in recs] == ["local"] and recs[0]["scores"] is None
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
    _config(workspace_dir, "")
    _, rid0 = _run(workspace_dir)
    _, rid0b = _run(workspace_dir)          # the same output twice: one handle, two records
    assert rid0 == rid0b
    ws = make_ws(workspace_dir)
    get(make_store(ws), ws, f"run:{rid0}#stdout", Selector(lines=(1, 3)))
    get(make_store(ws), ws, f"run:{rid0}#stdout", Selector(lines=(4, 6)))
    time.sleep(0.01)
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
    model, shadow, local = (report["modes"][k] for k in ("model", "shadow", "local"))
    assert (model["digests"], model["followed_by_fetch"], model["fetches"]) == (1, 1, 2)
    # each fetch counted once, not once per record of the handle
    assert (local["digests"], local["followed_by_fetch"], local["fetches"]) == (2, 1, 2)
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
    probe = ("','.join(m for m in ('ctx.digest.decision_client', "
             "'urllib.request', 'http.client', 'ssl') if m in sys.modules)")
    code = ("import sys\nfrom ctx.cli import main\n"
            "try:\n    main()\nexcept SystemExit:\n    pass\n"
            f"sys.stderr.write('\\nLOADED=' + {probe})\n")
    env = {"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin",
           "CTX_STATE_HOME": str(tmp_path / "state"), "HOME": str(tmp_path),
           "XDG_CONFIG_HOME": str(tmp_path / "xdg"), "CTX_PICKER_MODE": "model"}
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


# ---------------------------------------------------------------- where it may be enabled
def _ws_toml(workspace_dir, body: str) -> None:
    (workspace_dir / "ctx.toml").write_text("version = 1\n[picker]\n" + body, encoding="utf-8")


def test_a_workspace_ctx_toml_cannot_enable_the_picker(state_home, workspace_dir, stub,
                                                        monkeypatch):
    """The reviewer's repro: a cloned repository naming the endpoint and an
    environment variable must send nothing."""
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    _ws_toml(workspace_dir, f'mode = "model"\nendpoint = "{stub.url}"\n'
                            'api_key_env = "PYTHONHASHSEED"\nmodel = "x"\n')
    _run(workspace_dir)
    _run(workspace_dir)
    assert stub.requests == []
    ws = make_ws(workspace_dir)
    assert ws.config.picker.mode == "local" and ws.config.picker.endpoint != stub.url
    fails = [f for f in _failures(workspace_dir) if f["op"] == "config"]
    assert len(fails) == 1                      # recorded once, not per load
    assert "api_key_env" in fails[0]["detail"] and "endpoint" in fails[0]["detail"]
    assert "PYTHONHASHSEED" not in fails[0]["detail"] and stub.url not in fails[0]["detail"]


def test_user_level_config_enables_and_the_workspace_can_only_disable_or_lower(
        state_home, workspace_dir, stub, monkeypatch, tmp_path):
    other = _Stub()
    try:
        monkeypatch.setenv(KEY_ENV, KEY)
        path = _user_file(f'[picker]\nmode = "model"\nendpoint = "{stub.url}"\n'
                          f'api_key_env = "{KEY_ENV}"\n')
        _ws_toml(workspace_dir, "")
        _run(workspace_dir)
        assert len(stub.requests) == 1                      # user-level enable works
        assert make_ws(workspace_dir).config.picker.source == str(path)
        # the workspace cannot redirect it
        _ws_toml(workspace_dir, f'endpoint = "{other.url}"\nmax_candidates = 2\n')
        _run(workspace_dir)
        assert len(stub.requests) == 2 and other.requests == []
        assert len(stub.requests[-1]["doc"]["questions"]) <= 2   # lowering is honoured
        # ... but it can turn it off
        _ws_toml(workspace_dir, 'mode = "local"\n')
        _run(workspace_dir)
        assert len(stub.requests) == 2
        pol = make_ws(workspace_dir).config.picker
        assert pol.mode == "local" and "disabled by workspace ctx.toml" in pol.source
    finally:
        other.close()


def test_doctor_names_the_effective_mode_its_file_and_what_was_ignored(
        state_home, workspace_dir, monkeypatch):
    from ctx.installer import doctor_checks

    path = _user_file('[picker]\nmode = "shadow"\n')
    checks = {name: detail for name, _ok, detail in doctor_checks(make_ws(workspace_dir))}
    assert checks["evidence-map picker"].startswith(f"mode=shadow (from {path})")
    monkeypatch.setenv("CTX_PICKER_ENDPOINT", "https://elsewhere.example/x")
    checks = {name: detail for name, _ok, detail in doctor_checks(make_ws(workspace_dir))}
    assert "ignored: CTX_PICKER_ENDPOINT" in checks["evidence-map picker"]
    path.unlink()
    monkeypatch.delenv("CTX_PICKER_ENDPOINT")
    checks = {name: detail for name, _ok, detail in doctor_checks(make_ws(workspace_dir))}
    assert checks["evidence-map picker"] == "mode=local (from default)"


def test_the_environment_cannot_enable_the_picker_only_disable_or_lower(
        state_home, workspace_dir, stub, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    for k, v in (("MODE", "model"), ("ENDPOINT", stub.url), ("API_KEY_ENV", KEY_ENV),
                 ("MODEL", "x")):
        monkeypatch.setenv(f"CTX_PICKER_{k}", v)
    _run(workspace_dir)
    assert stub.requests == []
    fails = [f for f in _failures(workspace_dir) if f["op"] == "config"]
    assert len(fails) == 1 and "CTX_PICKER_ENDPOINT" in fails[0]["detail"]
    for k in ("MODE", "ENDPOINT", "API_KEY_ENV", "MODEL"):
        monkeypatch.delenv(f"CTX_PICKER_{k}")
    _config(workspace_dir, "model", stub.url)
    monkeypatch.setenv("CTX_PICKER_MAX_CANDIDATES", "2")
    _run(workspace_dir)
    assert len(stub.requests) == 1 and len(stub.requests[0]["doc"]["questions"]) <= 2
    monkeypatch.setenv("CTX_PICKER_MODE", "local")             # the kill switch
    _run(workspace_dir)
    assert len(stub.requests) == 1


def test_home_and_xdg_variables_do_not_locate_an_enabling_file(
        state_home, workspace_dir, stub, monkeypatch, tmp_path):
    monkeypatch.setenv(KEY_ENV, KEY)
    for base, sub in ((tmp_path / "fakehome", ".config/ctx"), (tmp_path / "fakexdg", "ctx")):
        d = base / sub
        d.mkdir(parents=True)
        (d / "config.toml").write_text(
            f'[picker]\nmode = "model"\nendpoint = "{stub.url}"\napi_key_env = "{KEY_ENV}"\n',
            encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "fakexdg"))
    _run(workspace_dir)
    assert stub.requests == []


def test_the_account_home_lookup_ignores_HOME(monkeypatch, tmp_path):
    import ctx.config as cfg

    monkeypatch.undo()                     # the real lookup, not the fixture's
    real = cfg.account_home()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cfg.account_home() == real and real != str(tmp_path)


def test_a_key_variable_outside_the_fixed_list_is_refused(state_home, workspace_dir, stub,
                                                          monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "should-never-leave")
    _user_file(f'[picker]\nmode = "model"\nendpoint = "{stub.url}"\napi_key_env = "GITHUB_TOKEN"\n')
    _run(workspace_dir)
    assert stub.requests == []
    pol = make_ws(workspace_dir).config.picker
    assert pol.mode == "local" and any("api_key_env must be one of" in n for n in pol.notes)
    assert any("api_key_env must be one of" in f["detail"] for f in _failures(workspace_dir))


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permissions")
def test_a_group_or_world_writable_user_file_is_ignored(state_home, workspace_dir, stub,
                                                        monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    f = _user_file(f'[picker]\nmode = "model"\nendpoint = "{stub.url}"\napi_key_env = "{KEY_ENV}"\n')
    f.chmod(0o666)
    _run(workspace_dir)
    assert stub.requests == []
    assert any("writable by group or others" in x["detail"] for x in _failures(workspace_dir))


# ---------------------------------------------------------------- transport guards
def test_a_redirect_is_refused_and_the_key_never_reaches_the_second_host(
        state_home, workspace_dir, stub, monkeypatch):
    target = _Stub()
    try:
        monkeypatch.setenv(KEY_ENV, KEY)
        _config(workspace_dir, "")
        local, _ = _run(workspace_dir)
        stub.behaviour = "redirect:" + target.url
        _config(workspace_dir, "model", stub.url)
        got, _ = _run(workspace_dir)
        assert got == local
        assert len(stub.requests) == 1 and target.requests == []
        assert _ledger(workspace_dir)[-1]["error"] == "redirect refused"
        assert [f["detail"] for f in _failures(workspace_dir) if f["op"] == "picker"] == \
            ["redirect refused"]
    finally:
        target.close()


def test_plain_http_is_refused_unless_loopback():
    from types import SimpleNamespace

    from ctx.digest.decision_client import DecisionClient
    from ctx.digest.picker import PickerError

    pol = SimpleNamespace(endpoint="http://decisions.example.invalid/v1", model="m",
                          api_key_env="CTX_TEST_ANY", timeout_ms=500)
    client = DecisionClient(pol)
    with pytest.raises(PickerError, match="plain http allowed only to a loopback endpoint"):
        client._request(b"{}", "k")


def test_scores_outside_0_1_are_malformed_in_the_client_and_in_the_picker(stub, monkeypatch):
    from types import SimpleNamespace

    from ctx.digest import evidence_map as E
    from ctx.digest.base import StreamView
    from ctx.digest.decision_client import DecisionClient
    from ctx.digest.picker import PickerError, PickerRun

    monkeypatch.setenv(KEY_ENV, KEY)
    stub.behaviour = "range"
    pol = SimpleNamespace(endpoint=stub.url, model="m", api_key_env=KEY_ENV, timeout_ms=2000,
                          mode="model", threshold=0.5, max_candidates=10, max_candidate_bytes=400)
    with pytest.raises(PickerError, match="malformed answer"):
        DecisionClient(pol).score("i", "c", ["a", "b"])

    class Bad:
        def score(self, intent, command, candidates):
            return [1.5] * len(candidates), None

    text = _log()
    view = StreamView("stdout", len(text), text.count("\n"), "text/plain", text, True)
    ctx = SimpleNamespace(stdout=view, stderr=StreamView("stderr", 0, 0, "text/plain", "", True),
                          focus_terms=())
    run = PickerRun(pol, None, "./ci.sh", None, scorer=Bad())
    assert run.choose(E.build(ctx), 3) == (None, None)
    assert run.record["error"] == "malformed answer"


def test_the_request_carries_only_what_the_digest_would_show(state_home, workspace_dir, stub,
                                                             monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    rows = _log().splitlines()
    pem_body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
    rows[497:500] = ["-----BEGIN RSA PRIVATE KEY-----", pem_body, "-----END RSA PRIVATE KEY-----"]
    rows[502] = "INFO dsn postgres://admin:SuperSecretPw99@db.internal/app"
    token = "ghp_" + "Z" * 36
    rows[501] = f"INFO retry with token {token}"
    _config(workspace_dir, "model", stub.url)
    ws = make_ws(workspace_dir)
    digest, _ = digest_output(make_store(ws), ws, "Bash", "\n".join(rows) + "\n",
                              argv=["./ci.sh"], picker=True)
    sent = json.dumps(stub.requests[0]["doc"])
    assert "rootcause" in sent                                  # the candidate went out ...
    assert "L499: " in sent and "L502: " in sent and "L503: " in sent
    for secret in (pem_body, "SuperSecretPw99", token):
        assert secret not in sent and secret not in digest      # ... sanitised


# ---------------------------------------------------------------- failure hook file read
def _persisted(tmp_path, session: str, data: bytes, *, folder="tool-results"):
    d = tmp_path / "claude" / "projects" / "proj" / session / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / "out.txt"
    f.write_bytes(data)
    return f


def _fail_payload(workspace_dir, session, path=None, size=None, error="Exit code 1\nshort"):
    p = {"cwd": str(workspace_dir), "session_id": session, "hook_event_name": "PostToolUseFailure",
         "tool_name": "Bash", "tool_input": {"command": "./ci.sh"}, "error": error}
    if path is not None:
        p["tool_response"] = {"persistedOutputPath": str(path), "persistedOutputSize": size}
    return p


def test_failure_hook_reads_only_the_host_provided_file(state_home, workspace_dir, tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    data = _log().encode()
    good = _persisted(tmp_path, "s1", data)
    out = _failure_hook(_fail_payload(workspace_dir, "s1", good, len(data)))
    assert "FATAL alpha crashed" in out["hookSpecificOutput"]["additionalContext"]
    # a path the output TEXT names is never read
    text = "Exit code 1\nFull output saved to: " + str(good)
    assert _failure_hook(_fail_payload(workspace_dir, "s1", error=text)) == {}
    # another session's file, a file outside tool-results, a wrong size
    other = _persisted(tmp_path, "s2", data)
    assert _failure_hook(_fail_payload(workspace_dir, "s1", other, len(data))) == {}
    outside = _persisted(tmp_path, "s1", data, folder="elsewhere")
    assert _failure_hook(_fail_payload(workspace_dir, "s1", outside, len(data))) == {}
    assert _failure_hook(_fail_payload(workspace_dir, "s1", good, len(data) + 1)) == {}


def test_failure_hook_bounds_the_bytes_it_reads(state_home, workspace_dir, tmp_path, monkeypatch):
    import ctx.hook as hook

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    data = _log().encode()
    good = _persisted(tmp_path, "s1", data)
    monkeypatch.setattr(hook, "_FAILURE_FULL_MAX", len(data) - 1)
    assert _failure_hook(_fail_payload(workspace_dir, "s1", good, len(data))) == {}


def test_failure_path_digest_is_no_larger_than_the_success_path(state_home, workspace_dir):
    ok, _ = _run(workspace_dir)
    ws = make_ws(workspace_dir)
    bad, _ = digest_output(make_store(ws), ws, "Bash", _log(), argv=["./ci.sh"], is_error=True)
    assert len(bad.encode()) <= len(ok.encode()) + 16
