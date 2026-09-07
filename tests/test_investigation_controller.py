"""Real repository edits and independent processes; only inference is a fixture."""
import json
import subprocess
import sys

import pytest

from conftest import make_ws, make_store
from ctx.controllers import investigation as ctl
from ctx.semantic.worker import WorkerResult
from ctx.task_runtime import TaskRuntime


@pytest.fixture
def case(git_workspace, state_home, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = git_workspace
    (root / "calc.py").write_text("def discount(amount):\n    return amount * 0.8\n")
    (root / "verify.py").write_text("from calc import discount\nassert discount(100) == 90\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "regression fixture"], cwd=root, check=True)
    ws = make_ws(root)
    store = make_store(ws)
    request = {"question": "Fix discount: a 100 purchase should cost 90", "scopes": ["calc.py", "verify.py"],
        "targets": ["calc.py"], "witnesses": ["verify.py"],
        "checks": [{"kind": "behavior", "argv": [sys.executable, "verify.py"]}],
        "worker": {"identity": "fixture/v1", "model": "fixture"},
        "limits": {"max_tokens": 1000000, "max_cost_usd": 3}}
    yield ws, store, request
    store.close()


class Agent:
    def __init__(self, *, wrong_first=False):
        self.calls = []
        self.latest_read = None
        self.wrong_first = wrong_first
        self.repair_count = 0

    def __call__(self, data, **kwargs):
        req = json.loads(data)
        self.calls.append(req["schema"])
        if req["schema"] == "ctx.semantic.request/v1":
            part = req["evidence"]
            result = {"schema": "ctx.semantic.response/v1", "findings": [{"summary": "Discount multiplier is incorrect",
                "support": [{"ref": part["ref"], "lines": [part["start"], part["end"]]}],
                "counterevidence": []}], "unresolved": []}
        else:
            stage = req["round"] % 3
            if stage == 0:
                action = {"action": "operation", "op": "evidence.read", "args": {"ref": "repo:calc.py", "start": 1, "end": 2}}
            elif stage == 1:
                self.latest_read = next(o["rows"][0] for o in reversed(req["observations"]) if o["op"] == "evidence.read")
                action = {"action": "operation", "op": "semantic.map",
                          "args": {"question": "Which multiplier satisfies the regression?", "sources": ["repo:calc.py"]}}
            else:
                self.repair_count += 1
                multiplier = "0.7" if self.wrong_first and self.repair_count == 1 else "0.9"
                v = self.latest_read
                action = {"action": "repair", "diagnosis": "Correct the multiplier", "counterevidence": [],
                    "support": [{"ref": v["ref"], "lines": [1, 2]}],
                    "edits": [{"path": "calc.py", "span": "2:2", "snapshot": v["snapshot"],
                               "replacement": f"    return amount * {multiplier}\n"}]}
            result = {"schema": "ctx.investigation.decision/v1", "decision": action}
        return WorkerResult(json.dumps(result).encode(), returncode=0)


def test_hunt_investigate_fix_and_explicit_apply(case):
    ws, store, request = case
    agent = Agent()
    task = ctl.prepare(ws, store, request)
    ref, report = ctl.run(ws, store, task, worker=agent)
    assert report["status"] == "verified", report
    assert report["totals"]["calls"] == 4
    assert "0.8" in (ws.root / "calc.py").read_text()  # active checkout untouched
    assert b"+    return amount * 0.9" in store.get_blob(report["patch"][5:])
    assert report["baseline"][0]["failed"]
    assert report["totals"]["reported_cost_usd"] is None
    ctl.run(ws, store, task, worker=lambda *a, **k: pytest.fail("replayed finished task"))
    assert ctl.apply(ws, store, task)["outcome"] == "applied"
    assert "0.9" in (ws.root / "calc.py").read_text()


def test_failed_patch_feeds_back_into_investigation(case):
    ws, store, request = case
    agent = Agent(wrong_first=True)
    _, report = ctl.run(ws, store, ctl.prepare(ws, store, request), worker=agent)
    assert report["status"] == "verified", report
    assert len(report["repairs"]) == 2
    first = json.loads(store.get_blob(report["repairs"][0]["verification"][5:]))
    assert first["outcome"] == "failed"
    assert report["totals"]["calls"] == 8


def test_passing_baseline_never_claims_a_fix(case):
    ws, store, request = case
    request["checks"][0]["argv"] = [sys.executable, "-c", "pass"]
    _, report = ctl.run(ws, store, ctl.prepare(ws, store, request),
                       worker=lambda *a, **kw: pytest.fail("unnecessary inference"))
    assert report["status"] == "not_reproduced"
    assert report["patch"] is None


def test_resume_preserves_evidence_and_model_reservations(case):
    ws, store, request = case
    task = ctl.prepare(ws, store, request)
    agent = Agent()
    calls = [0]
    def interrupted(data, **kw):
        calls[0] += 1
        if calls[0] == 2:
            raise KeyboardInterrupt
        return agent(data, **kw)
    with pytest.raises(KeyboardInterrupt):
        ctl.run(ws, store, task, worker=interrupted)
    partial = ctl.inspect(ws, store, task)[1]
    _, paused = ctl.run(ws, store, task, worker=agent)
    assert paused["stop_reason"] == "retry_requires_explicit_request"
    _, report = ctl.run(ws, store, task, worker=agent, retry_failed=True)
    assert report["status"] == "verified", report
    assert report["totals"]["calls"] == 5
    assert report["baseline"] == partial["baseline"]
    assert report["totals"]["charged_seconds"] >= 60


def test_shared_budget_stops_investigation_before_extra_calls(case):
    ws, store, request = case
    request["limits"]["max_calls"] = 2
    _, report = ctl.run(ws, store, ctl.prepare(ws, store, request), worker=Agent())
    assert report["status"] == "paused"
    assert report["stop_reason"] == "call_budget"
    assert report["totals"]["calls"] == 2
    assert not report["repairs"]


def test_uncertain_applied_edit_is_reconciled_without_reapplying(case, monkeypatch):
    from ctx import taskledger
    ws, store, request = case
    task = ctl.prepare(ws, store, request)
    original = taskledger.append
    interrupted = [False]
    def crash(root, row, **kw):
        if row.get("op") == "edit.apply" and row.get("status") == "done" and not interrupted[0]:
            interrupted[0] = True
            raise KeyboardInterrupt
        return original(root, row, **kw)
    monkeypatch.setattr(taskledger, "append", crash)
    agent = Agent()
    with pytest.raises(KeyboardInterrupt):
        ctl.run(ws, store, task, worker=agent)
    monkeypatch.setattr(ctl, "apply_edit_plan", lambda *a, **k: pytest.fail("repeated committed edit"))
    _, report = ctl.run(ws, store, task, worker=agent, retry_failed=True)
    assert report["status"] == "verified", report
    assert any(a.get("recovered") for a in report["operations"])


def test_verified_task_survives_dependency_gc(case):
    ws, store, request = case
    task = ctl.prepare(ws, store, request)
    _, report = ctl.run(ws, store, task, worker=Agent())
    assert report["status"] == "verified"
    rt = TaskRuntime(ws, store, task)
    rt.retain()
    latest = store.db.execute("SELECT id FROM objects WHERE kind='execution' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    with store.db:
        store.db.execute("UPDATE objects SET created_at=0")
        store.db.execute("DELETE FROM leases WHERE id != ?", (latest,))
    store.gc(0)
    # Receipt -> snapshot/diagnostic/run -> output dependencies all survive.
    assert ctl.run(ws, store, task, worker=lambda *a, **k: pytest.fail("replayed after GC"))[1]["status"] == "verified"


def test_verification_witness_changes_refuse_continuation(case):
    from pathlib import Path
    ws, store, request = case
    request["limits"]["max_calls"] = 1
    task = ctl.prepare(ws, store, request)
    _, report = ctl.run(ws, store, task, worker=Agent())
    (Path(report["worktree"]) / "verify.py").write_text("pass\n")
    _, report = ctl.run(ws, store, task, worker=Agent())
    assert report["status"] == "paused"
    assert report["stop_reason"] == "verification_inputs_changed"


def test_completed_task_can_recover_after_freshness_is_restored(case):
    from pathlib import Path
    ws, store, request = case
    task = ctl.prepare(ws, store, request)
    _, report = ctl.run(ws, store, task, worker=Agent())
    target = Path(report["worktree"]) / "calc.py"
    verified_bytes = target.read_bytes()
    target.write_text("pass\n")
    no_model = lambda *a, **k: pytest.fail("replayed a completed task")
    _, paused = ctl.run(ws, store, task, worker=no_model)
    assert paused["stop_reason"] == "worktree_changed"
    target.write_bytes(verified_bytes)
    _, restored = ctl.run(ws, store, task, worker=no_model)
    assert restored["status"] == "verified"
    assert restored["patch"] == report["patch"]
    assert restored["totals"]["calls"] == report["totals"]["calls"]


def test_prepared_task_refuses_changed_workspace_policy(case):
    from dataclasses import replace
    ws, store, request = case
    task = ctl.prepare(ws, store, request)
    altered = replace(ws, ignore_globs=(*ws.ignore_globs, "new-exclusion"))
    with pytest.raises(ValueError, match="binding changed"):
        ctl.run(altered, store, task, worker=Agent())


def test_model_success_statement_cannot_bypass_checks(case):
    ws, store, request = case
    def unsupported(data, **kw):
        return WorkerResult(b'{"schema":"ctx.investigation.decision/v1","decision":{"action":"success","fixed":true}}')
    _, report = ctl.run(ws, store, ctl.prepare(ws, store, request), worker=unsupported)
    assert report["status"] == "paused"
    assert report["stop_reason"] == "decision_failed"
    assert report["patch"] is None


def test_acp_adapter_and_cli_execute_the_complete_task(case, tmp_path, monkeypatch, capsys):
    from ctx.acp import configure
    from ctx.cli import main
    from pathlib import Path
    ws, store, request = case
    server = tmp_path / "agent.py"
    log = tmp_path / "wire.jsonl"
    server.write_text('''
import json, sys, os

def send(value):
    print(json.dumps({"jsonrpc":"2.0", **value}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    with open(os.environ["WIRE_LOG"], "a") as f: f.write(line)
    method = msg.get("method")
    if method == "initialize":
        send({"id":msg["id"], "result":{"protocolVersion":1}})
    elif method == "session/new":
        assert msg["params"]["mcpServers"] == []
        send({"id":msg["id"], "result":{"sessionId":"s", "models":{"currentModelId":"fixture", "availableModels":[{"modelId":"fixture"}]}}})
    elif method == "session/prompt":
        r = json.loads(msg["params"]["prompt"][0]["text"])
        if r["schema"] == "ctx.semantic.request/v1":
            p=r["evidence"]
            result={"schema":"ctx.semantic.response/v1","findings":[{"summary":"Wrong multiplier","support":[{"ref":p["ref"],"lines":[p["start"],p["end"]]}],"counterevidence":[]}],"unresolved":[]}
        else:
            if r["round"] == 0:
                action={"action":"operation","op":"evidence.read","args":{"ref":"repo:calc.py","start":1,"end":2}}
            elif r["round"] == 1:
                action={"action":"operation","op":"semantic.map","args":{"question":"Inspect multiplier","sources":["repo:calc.py"]}}
            else:
                v=next(o["rows"][0] for o in r["observations"] if o["op"] == "evidence.read")
                action={"action":"repair","diagnosis":"Fix multiplier","support":[{"ref":v["ref"],"lines":[1,2]}],"counterevidence":[],"edits":[{"path":"calc.py","span":"2:2","snapshot":v["snapshot"],"replacement":"    return amount * 0.9\\n"}]}
            result={"schema":"ctx.investigation.decision/v1","decision":action}
        result["usage"]={"cost_usd":99999,"input_tokens":10000000}
        send({"method":"session/update","params":{"sessionId":"s","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":json.dumps(result)}}}})
        send({"id":msg["id"],"result":{"stopReason":"end_turn"}})
    elif method == "session/cancel":
        break
''')
    monkeypatch.setenv("WIRE_LOG", str(log))
    configure(ws.root, "codex", "fixture", command=[sys.executable, str(server)])
    subprocess.run(["git", "add", ".ctx/acp.json"], cwd=ws.root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture endpoint"], cwd=ws.root, check=True)
    ws = make_ws(ws.root)
    request.pop("worker")
    request["host"] = "codex"
    task = ctl.prepare(ws, store, request)
    assert main(["--workspace", str(ws.root), "task", "run", task]) == 0
    output = capsys.readouterr().out
    assert "verified" in output
    report = ctl.inspect(ws, store, task)[1]
    assert report["totals"]["reported_cost_usd"] is None  # generated usage was discarded
    assert report["totals"]["calls"] == 4
    wire = [json.loads(s) for s in log.read_text().splitlines()]
    assert all(Path(m["params"]["cwd"]) != Path(report["worktree"]) for m in wire if m.get("method") == "session/new")
