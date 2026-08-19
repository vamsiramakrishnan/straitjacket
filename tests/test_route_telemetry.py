from __future__ import annotations

import json

import pytest

from ctx.route_telemetry import (
    append_route_label,
    export_route_observations,
    labeled_route_runs,
    load_route_runs,
    route_summary,
)
from ctx.sessiondir import session_reads_path
from evals.alphaevolve.route_replay.snapshot import build_snapshot


def _write_run(root, run_id: str = "route-1") -> None:
    path = session_reads_path(root, "route.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "ctx.route-run/v1",
                "run_id": run_id,
                "task_profile": {"kind": "test"},
                "measurement": {
                    "task_success": "unmeasured",
                    "actual_usage": {
                        "status": "available",
                        "total_tokens": 123,
                        "cost_usd": 0.004,
                    },
                },
            }
        )
        + "\n{malformed\n",
        encoding="utf-8",
    )


def test_route_labels_are_explicit_append_only_semantic_evidence(tmp_path):
    _write_run(tmp_path)
    label = append_route_label(
        tmp_path,
        "route-1",
        task_success=True,
        evidence_kind="named_test",
    )
    assert label["task_success"] is True
    joined = labeled_route_runs(tmp_path)
    assert joined[0]["measurement"]["task_success"] == "unmeasured"
    assert joined[0]["semantic_label"] == {
        "task_success": True,
        "evidence_kind": "named_test",
    }
    assert route_summary(tmp_path) == {
        "schema": "ctx.route-summary/v1",
        "runs": 1,
        "labeled": 1,
        "unlabeled": 0,
        "successes": 1,
        "failures": 0,
        "actual_usage": {"available": 1, "partial": 0, "unavailable": 0},
        "kinds": {"test": 1},
    }


def test_route_label_rejects_unknown_run_and_unbounded_evidence_vocabulary(tmp_path):
    _write_run(tmp_path)
    with pytest.raises(ValueError, match="unknown route run"):
        append_route_label(
            tmp_path,
            "route-missing",
            task_success=True,
            evidence_kind="named_test",
        )
    with pytest.raises(ValueError, match="unsupported evidence kind"):
        append_route_label(
            tmp_path,
            "route-1",
            task_success=True,
            evidence_kind="free-form secret note",
        )


def test_route_reader_skips_malformed_and_wrong_schema_rows(tmp_path):
    _write_run(tmp_path)
    path = session_reads_path(tmp_path, "route.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema": "other", "run_id": "route-2"}) + "\n")
    assert [row["run_id"] for row in load_route_runs(tmp_path)] == ["route-1"]


def test_export_route_observations_excludes_task_and_outputs(tmp_path):
    _write_run(tmp_path)
    append_route_label(
        tmp_path, "route-1", task_success=True, evidence_kind="named_test"
    )
    exported = export_route_observations(tmp_path)
    rendered = json.dumps(exported)
    assert exported["schema"] == "ctx.route-replay-observations/v1"
    assert "task" not in exported["observations"][0]
    assert "outcomes" not in exported["observations"][0]
    assert "stdout" not in rendered
    assert exported["observations"][0]["measurement"]["actual_usage"] == {
        "status": "available",
        "total_tokens": 123,
        "cost_usd": 0.004,
    }


def test_snapshot_refresh_preserves_reviewed_disposable_workspace_rows(tmp_path):
    frozen = tmp_path / "observations.json"
    frozen.write_text(
        json.dumps(
            {
                "schema": "ctx.route-replay-observations/v1",
                "observations": [{"run_id": "disposed-run", "label": {}}],
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "active"
    workspace.mkdir()
    _write_run(workspace, "active-run")
    append_route_label(
        workspace, "active-run", task_success=True, evidence_kind="named_test"
    )
    snapshot = build_snapshot([workspace], existing=frozen)
    assert [row["run_id"] for row in snapshot["observations"]] == [
        "active-run",
        "disposed-run",
    ]
