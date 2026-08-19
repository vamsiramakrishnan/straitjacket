"""Read and annotate privacy-safe orchestration receipts.

Route execution writes structural data to ``route.jsonl``. Semantic success is
never inferred from a process exit: a separate append-only label ledger records
an explicit pass/fail decision and its bounded evidence category.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from ctx.sessiondir import session_reads_path

ROUTE_RUN_SCHEMA = "ctx.route-run/v1"
ROUTE_LABEL_SCHEMA = "ctx.route-label/v1"
_EVIDENCE_KINDS = frozenset(
    {
        "acceptance_check",
        "named_test",
        "reviewed_output",
        "user_confirmation",
        "known_failure",
    }
)


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    doc = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(doc, dict):
                    yield doc
    except OSError:
        return


def load_route_runs(workspace_root: Path) -> list[dict[str, Any]]:
    """Return valid route receipts in append order; malformed rows are skipped."""
    return [
        row
        for row in _jsonl(session_reads_path(workspace_root, "route.jsonl"))
        if row.get("schema") == ROUTE_RUN_SCHEMA and isinstance(row.get("run_id"), str)
    ]


def load_route_labels(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """Return the last valid label per run ID."""
    labels: dict[str, dict[str, Any]] = {}
    for row in _jsonl(session_reads_path(workspace_root, "route-labels.jsonl")):
        run_id = row.get("run_id")
        if row.get("schema") == ROUTE_LABEL_SCHEMA and isinstance(run_id, str):
            labels[run_id] = row
    return labels


def append_route_label(
    workspace_root: Path,
    run_id: str,
    *,
    task_success: bool,
    evidence_kind: str,
) -> dict[str, Any]:
    """Append an explicit semantic result for an existing route receipt."""
    runs = {row["run_id"] for row in load_route_runs(workspace_root)}
    if run_id not in runs:
        raise ValueError(f"unknown route run: {run_id}")
    if evidence_kind not in _EVIDENCE_KINDS:
        raise ValueError(f"unsupported evidence kind: {evidence_kind}")
    label = {
        "schema": ROUTE_LABEL_SCHEMA,
        "run_id": run_id,
        "recorded_at_unix": time.time(),
        "task_success": bool(task_success),
        "evidence_kind": evidence_kind,
    }
    path = session_reads_path(workspace_root, "route-labels.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(label, sort_keys=True) + "\n")
    return label


def labeled_route_runs(workspace_root: Path) -> list[dict[str, Any]]:
    """Join route receipts to explicit semantic labels without mutating either."""
    labels = load_route_labels(workspace_root)
    joined: list[dict[str, Any]] = []
    for run in load_route_runs(workspace_root):
        label = labels.get(run["run_id"])
        if label is None:
            continue
        doc = dict(run)
        doc["semantic_label"] = {
            "task_success": label["task_success"],
            "evidence_kind": label["evidence_kind"],
        }
        joined.append(doc)
    return joined


def route_summary(workspace_root: Path) -> dict[str, Any]:
    """Compact instrumentation health and outcome summary."""
    runs = load_route_runs(workspace_root)
    labels = load_route_labels(workspace_root)
    labeled = [labels[row["run_id"]] for row in runs if row["run_id"] in labels]
    usage_statuses = [
        (
            row.get("measurement", {}).get("actual_usage", {}).get("status")
            if isinstance(row.get("measurement", {}).get("actual_usage"), dict)
            else "unavailable"
        )
        for row in runs
    ]
    return {
        "schema": "ctx.route-summary/v1",
        "runs": len(runs),
        "labeled": len(labeled),
        "unlabeled": len(runs) - len(labeled),
        "successes": sum(1 for row in labeled if row.get("task_success") is True),
        "failures": sum(1 for row in labeled if row.get("task_success") is False),
        "actual_usage": {
            status: sum(1 for value in usage_statuses if value == status)
            for status in ("available", "partial", "unavailable")
        },
        "kinds": {
            kind: sum(
                1 for row in runs if row.get("task_profile", {}).get("kind") == kind
            )
            for kind in sorted(
                {
                    str(row.get("task_profile", {}).get("kind", "unknown"))
                    for row in runs
                }
            )
        },
    }


def export_route_observations(workspace_root: Path) -> dict[str, Any]:
    """Build a reproducible, prompt-free evaluator snapshot from labeled runs.

    Only structural task fields, selected host/model, bounded measurements, and
    explicit semantic labels cross this boundary. Node goals, output, checkpoint
    contents, and the original task are deliberately absent.
    """
    observations: list[dict[str, Any]] = []
    for run in labeled_route_runs(workspace_root):
        profile = run.get("task_profile", {})
        measurement = run.get("measurement", {})
        nodes = run.get("route", {}).get("nodes", [])
        label = run["semantic_label"]
        exported_profile = {
            key: profile.get(key)
            for key in (
                "kind",
                "high_confidence",
                "mutation",
                "review",
                "verification_required",
                "characters",
                "words",
                "multiline",
                "named_target",
                "named_acceptance",
                "high_risk_scope",
                "explicit_contract",
            )
        }
        if measurement.get("verification_required") is not None:
            exported_profile["mutation"] = bool(
                measurement.get("verification_required")
            )
            exported_profile["verification_required"] = bool(
                measurement.get("verification_required")
            )
        observations.append(
            {
                "run_id": run["run_id"],
                "profile": exported_profile,
                "route": [
                    {
                        "role": node.get("role"),
                        "host": node.get("host"),
                        "model": node.get("model"),
                    }
                    for node in nodes
                    if isinstance(node, dict)
                ],
                "measurement": {
                    "route_completed": measurement.get("route_completed"),
                    "duration_ms": measurement.get("duration_ms"),
                    "estimated_spend_usd": measurement.get("estimated_spend_usd"),
                    "actual_usage": measurement.get("actual_usage", "unavailable"),
                    "waves": measurement.get("waves"),
                    "replans": measurement.get("replans"),
                },
                "label": {
                    "task_success": label["task_success"],
                    "evidence_kind": label["evidence_kind"],
                },
            }
        )
    return {
        "schema": "ctx.route-replay-observations/v1",
        "observations": observations,
    }


__all__ = [
    "ROUTE_LABEL_SCHEMA",
    "ROUTE_RUN_SCHEMA",
    "append_route_label",
    "export_route_observations",
    "labeled_route_runs",
    "load_route_labels",
    "load_route_runs",
    "route_summary",
]
