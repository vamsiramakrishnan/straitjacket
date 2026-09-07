"""Semantic evidence contracts: exact coverage, grounded coordinates, honest cost."""
import json
from pathlib import Path
import sys

import pytest

from conftest import make_store, make_ws
from ctx.semantic import SemanticError, inspect, prepare, run
from ctx.semantic.contract import parse_json
from ctx.semantic.evidence import read_document, publish
from ctx.semantic.worker import WorkerResult


@pytest.fixture
def setup(workspace_dir, state_home):
    (workspace_dir / "a.py").write_text("def first():\n    return 1\n\ndef second():\n    return 2\n")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    spec = {"question": "Which functions return constants?", "sources": ["repo:a.py"],
            "worker": {"identity": "fixture-v1", "model": "fixture", "command": [sys.executable, "-c", "pass"]},
            "limits": {"partition_bytes": 30, "reserve_cost_usd": 0.1}}
    yield ws, store, spec
    store.close()


def answer(request, *, usage=None, summary="Returns a constant"):
    value = json.loads(request)
    part = value["evidence"]
    return {"schema": "ctx.semantic.response/v1", "findings": [
        {"summary": summary, "support": [{"ref": part["ref"], "lines": [part["start"], part["end"]]}],
         "counterevidence": []}], "unresolved": ["Check callers"],
        "usage": usage if usage is not None else {"input_tokens": 50, "output_tokens": 10, "cost_usd": 0.02}}


def success(request, **kwargs):
    return WorkerResult(json.dumps(answer(request)).encode(), returncode=0)


def test_prepare_freezes_full_coverage_and_resume_ignores_worktree_changes(setup):
    ws, store, spec = setup
    handle = prepare(ws, store, spec)
    plan = read_document(store, handle)
    assert len(plan["partitions"]) == 2
    assert prepare(ws, store, spec) == handle
    assert b"".join(store.read_blob_lines(p["ref"][5:], p["start"], p["end"])
                    for p in plan["partitions"]) == (ws.root / "a.py").read_bytes()
    (ws.root / "a.py").write_text("totally different\n")
    ref, report = run(ws, store, handle, worker=success)
    assert report["coverage"]["processing_complete"] is True
    assert report["coverage"]["selection_complete"] is None
    assert report["epistemic_status"] == "model_inference"
    assert report["totals"]["calls"] == 2
    assert report["totals"]["reported_cost_usd"] == 0.04
    assert report["totals"]["admitted_cost_usd"] == 0.2
    assert read_document(store, ref) == report
    assert run(ws, store, handle, worker=lambda *a, **k: pytest.fail("replayed paid call"))[1] == report
    assert inspect(ws, store, handle)[1] == report


@pytest.mark.parametrize("change", ["question", "model", "settings", "identity", "sample"])
def test_inference_identity_includes_every_declared_input(setup, change):
    ws, store, spec = setup
    first = prepare(ws, store, spec)
    if change in {"model", "identity"}:
        spec["worker"][change] = "different"
    elif change == "settings":
        spec["worker"][change] = {"temperature": 0.5}
    else:
        spec[change] = "different"
    assert prepare(ws, store, spec) != first


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1, 0, True, "32", 1025])
def test_invalid_root_budget_rejected_before_spend(setup, bad):
    ws, store, spec = setup
    spec["limits"]["max_calls"] = bad
    with pytest.raises(SemanticError):
        prepare(ws, store, spec)


def test_source_and_line_limits_refuse_without_silent_truncation(setup):
    ws, store, spec = setup
    spec["limits"]["source_bytes"] = 3
    with pytest.raises(SemanticError, match="source_bytes"):
        prepare(ws, store, spec)
    spec["limits"].pop("source_bytes")
    spec["limits"]["partition_bytes"] = 3
    with pytest.raises(SemanticError, match="one evidence line"):
        prepare(ws, store, spec)


def test_non_utf8_binary_directory_and_ignored_inputs_are_refused(setup):
    ws, store, spec = setup
    for data in (b"\x80", b"a\0b"):
        (ws.root / "a.py").write_bytes(data)
        with pytest.raises(SemanticError):
            prepare(ws, store, spec)
    spec["sources"] = ["repo:"]
    with pytest.raises(SemanticError):
        prepare(ws, store, spec)
    (ws.root / ".env").write_text("secret")
    spec["sources"] = ["repo:.env"]
    with pytest.raises(SemanticError):
        prepare(ws, store, spec)


def test_unicode_line_boundaries_are_exact_and_empty_selection_needs_no_call(setup):
    ws, store, spec = setup
    data = "a\u2028b\r\nc\n".encode()
    spec["sources"] = ["blob:" + store.put_blob(data)]
    plan = read_document(store, prepare(ws, store, spec))
    assert plan["partitions"][0]["end"] == 2
    spec["sources"] = ["blob:" + store.put_blob(b"")]
    handle = prepare(ws, store, spec)
    _, report = run(ws, store, handle, worker=lambda *a, **k: pytest.fail("empty call"))
    assert report["coverage"]["processing_complete"]
    assert report["coverage"]["selected_bytes"] == 0
    assert report["totals"]["calls"] == 0


@pytest.mark.parametrize("mutation", ["ref", "range", "bool", "no_support", "extra_field"])
def test_invalid_citations_never_count_as_processed_but_keep_usage_and_raw_output(setup, mutation):
    ws, store, spec = setup
    def invalid(request, **kwargs):
        out = answer(request)
        finding = out["findings"][0]
        if mutation == "ref":
            finding["support"][0]["ref"] = "repo:a.py"
        elif mutation == "range":
            finding["support"][0]["lines"] = [1, 999]
        elif mutation == "bool":
            finding["support"][0]["lines"] = [True, 2]
        elif mutation == "no_support":
            finding["support"] = []
        else:
            finding["verified"] = True
        return WorkerResult(json.dumps(out).encode())
    handle = prepare(ws, store, spec)
    _, report = run(ws, store, handle, worker=invalid)
    assert report["status"] == "partial"
    assert not report["findings"]
    assert report["coverage"]["processed_bytes"] == 0
    assert report["totals"]["reported_cost_usd"] == 0.04
    assert store.get_blob(report["attempts"][0]["stdout"][5:])
    assert run(ws, store, handle, worker=success)[1] == report  # explicit retry required


def test_call_budget_survives_repeated_resume(setup):
    ws, store, spec = setup
    spec["limits"]["max_calls"] = 1
    handle = prepare(ws, store, spec)
    _, report = run(ws, store, handle, worker=success)
    assert report["coverage"]["completed_partitions"] == 1
    assert report["stop_reason"] == "call_budget"
    assert run(ws, store, handle, worker=success)[1] == report


def test_unknown_usage_is_never_free_and_cost_budget_survives_resume(setup):
    ws, store, spec = setup
    spec["limits"]["max_cost_usd"] = 0.1
    def unknown(data, **kwargs):
        return WorkerResult(json.dumps(answer(data, usage={})).encode())
    handle = prepare(ws, store, spec)
    _, report = run(ws, store, handle, worker=unknown)
    assert report["stop_reason"] == "cost_admission_budget"
    assert report["totals"]["reported_cost_usd"] is None
    assert report["totals"]["input_tokens"] is None
    assert report["totals"]["unknown_cost_calls"] == 1
    assert report["totals"]["admitted_cost_usd"] == 0.1
    assert run(ws, store, handle, worker=success)[1] == report


def test_actual_overspend_stops_subsequent_dispatch(setup):
    ws, store, spec = setup
    def expensive(data, **kwargs):
        return WorkerResult(json.dumps(answer(data, usage={"cost_usd": 5})).encode())
    _, report = run(ws, store, prepare(ws, store, spec), worker=expensive)
    assert report["totals"]["calls"] == 1
    assert report["totals"]["admitted_cost_usd"] == 5
    assert report["stop_reason"] == "cost_admission_budget"


def test_interruption_is_durable_and_retry_consumes_another_reservation(setup):
    ws, store, spec = setup
    handle = prepare(ws, store, spec)
    def interrupt(data, **kwargs):
        assert inspect(ws, store, handle)[1]["attempts"][0]["status"] == "running"
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(ws, store, handle, worker=interrupt)
    partial = inspect(ws, store, handle)[1]
    assert partial["attempts"][0]["status"] == "uncertain"
    assert partial["totals"]["unknown_cost_calls"] == 1
    _, report = run(ws, store, handle, worker=success, retry_failed=True)
    assert report["status"] == "complete"
    assert report["totals"]["calls"] == 3
    assert report["totals"]["admitted_cost_usd"] == pytest.approx(0.3)
    assert report["totals"]["reported_cost_usd"] is None


def test_concurrent_run_cannot_launch_against_the_same_budget(setup):
    ws, store, spec = setup
    handle = prepare(ws, store, spec)
    def nested(data, **kwargs):
        with pytest.raises(SemanticError, match="already running"):
            run(ws, store, handle[:20], worker=success)
        return success(data)
    assert run(ws, store, handle, worker=nested)[1]["status"] == "complete"


def test_tampered_partitions_are_rejected_before_dispatch(setup):
    ws, store, spec = setup
    plan = read_document(store, prepare(ws, store, spec))
    plan["partitions"][0]["end"] = 999
    fake = publish(store, plan)
    with pytest.raises(SemanticError, match="coverage"):
        run(ws, store, fake, worker=success)


def test_redaction_is_frozen_in_a_separate_citable_view_and_policy_changes_refuse(setup):
    from dataclasses import replace
    ws, store, spec = setup
    secret = "sk-" + "A" * 48
    (ws.root / "a.py").write_text(secret + "\n")
    spec["limits"]["partition_bytes"] = 1000
    handle = prepare(ws, store, spec)
    source = read_document(store, handle)["sources"][0]
    assert source["transformed"]
    assert source["ref"] != source["source"]
    assert secret.encode() not in store.get_blob(source["ref"][5:])
    def check(data, **kwargs):
        assert secret.encode() not in data
        return success(data)
    assert run(ws, store, handle, worker=check)[1]["status"] == "complete"
    ws.config = replace(ws.config, redaction=replace(ws.config.redaction, enabled=False))
    with pytest.raises(SemanticError, match="policy changed"):
        run(ws, store, handle, worker=success)


def test_retained_report_roots_all_evidence_and_attempt_blobs(setup):
    ws, store, spec = setup
    handle = prepare(ws, store, spec)
    report_ref, report = run(ws, store, handle, worker=success)
    # Expire every older lease, leaving only the final report's retention.
    latest = store.db.execute("SELECT id FROM objects WHERE kind='semantic' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    with store.db:
        store.db.execute("UPDATE objects SET created_at=0")
        store.db.execute("DELETE FROM leases WHERE id != ?", (latest,))
    store.gc(0)
    for ref in [handle, report_ref, *[s[k] for s in report["sources"] for k in ("source", "ref")],
                *[a[k] for a in report["attempts"] for k in ("request", "stdout", "stderr", "result")]]:
        store.get_blob(ref[5:])
    assert inspect(ws, store, handle)[1]["status"] == "complete"


def test_token_admission_budget_and_unknown_tokens_are_explicit(setup):
    ws, store, spec = setup
    spec["limits"]["max_tokens"] = 1
    _, report = run(ws, store, prepare(ws, store, spec), worker=success)
    assert report["stop_reason"] == "token_admission_budget"
    assert report["totals"]["calls"] == 0


def test_repeated_handles_are_not_reprocessed(setup):
    ws, store, spec = setup
    raw = (ws.root / "a.py").read_bytes()
    spec["sources"].append("repo:a.py")
    spec["limits"]["source_bytes"] = len(raw)
    plan = read_document(store, prepare(ws, store, spec))
    assert len(plan["sources"]) == 1
    assert len(plan["partitions"]) == 2


def test_identical_files_keep_their_distinct_locations(setup):
    ws, store, spec = setup
    (ws.root / "b.py").write_bytes((ws.root / "a.py").read_bytes())
    spec["sources"].append("repo:b.py")
    plan = read_document(store, prepare(ws, store, spec))
    assert {s["label"] for s in plan["sources"]} == {"repo:a.py", "repo:b.py"}
    assert len({s["ref"] for s in plan["sources"]}) == 1


def test_worker_wall_allowance_survives_resume(setup, monkeypatch):
    from types import SimpleNamespace
    from ctx.semantic import engine
    ws, store, spec = setup
    spec["limits"].update(wall_seconds=1.0, call_seconds=1.0)
    clock = [0.0]
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def slow(data, **kwargs):
        assert kwargs["timeout"] <= 1.0
        clock[0] += 1.0
        return success(data)
    handle = prepare(ws, store, spec)
    _, report = run(ws, store, handle, worker=slow)
    assert report["totals"]["calls"] == 1
    assert report["stop_reason"] == "wall_budget"
    assert run(ws, store, handle, worker=success)[1] == report


def test_killed_coordinator_keeps_unknown_call_and_time_reservations(setup):
    import os
    import subprocess
    ws, store, spec = setup
    spec["limits"].update(wall_seconds=1.0, call_seconds=1.0, max_calls=1)
    handle = prepare(ws, store, spec)
    code = """import os, sys
from ctx.semantic import run
from ctx.store import Store
from ctx.workspace import resolve_workspace
ws = resolve_workspace(sys.argv[1])
def crash(*args, **kwargs): os._exit(9)
run(ws, Store(ws.workspace_id), sys.argv[2], worker=crash)
"""
    # Independent interpreter, same on-disk store; no Python finally block runs.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    exited = subprocess.run([sys.executable, "-c", code, str(ws.root), handle], env=env, timeout=10)
    assert exited.returncode == 9
    _, report = run(ws, store, handle, worker=lambda *a, **k: pytest.fail("uncertain call was repeated"))
    assert report["attempts"][0]["status"] == "uncertain"
    assert report["totals"]["reported_cost_usd"] is None
    assert report["totals"]["elapsed_seconds"] is None
    assert 0.9 <= report["totals"]["charged_seconds"] <= 1.0
    assert report["stop_reason"] == "call_budget"


def test_sdk_worker_does_not_need_a_dummy_command(setup):
    ws, store, spec = setup
    spec["worker"].pop("command")
    handle = prepare(ws, store, spec)
    with pytest.raises(SemanticError, match="no command driver"):
        run(ws, store, handle)
    assert run(ws, store, handle, worker=success)[1]["status"] == "complete"


def test_duplicate_and_nonfinite_json_are_rejected():
    for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'):
        with pytest.raises(SemanticError):
            parse_json(data)
