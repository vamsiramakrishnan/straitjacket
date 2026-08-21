#!/usr/bin/env python3
"""Frozen matched canary for the oh-my-pi mechanism wave.

The edit arm holds the proposed replacement constant and compares a naive line
coordinate write with ctx's sealed edit transaction after deterministic drift.
The orchestration arm drives the production route/worktree implementation with
deterministic workers.  Optional live hosts propose the replacement once; the
same proposal is replayed through both edit arms, avoiding model variance.

Live is opt-in and bounded.  Transcripts are not persisted; only structured
usage, timing, outcome, and host metadata enter the receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ctx import anchors, hosts
from ctx.config import OrchestratePolicy
from ctx.edit_transactions import (
    REQUEST_SCHEMA,
    EditTransactionError,
    apply_edit_plan,
    create_edit_plan,
)
from ctx.orchestrator import _launch_host, build_route_plan, run_route
from ctx.store import Store
from ctx.workspace import resolve_workspace

SCHEMA = "ctx.oh-my-pi-canary/v1"
FROZEN_TASKSET = "oh-my-pi-edit-drift/v1"
DEFAULT_REPLACEMENT = "target = 2\n"


@dataclass(frozen=True)
class EditCase:
    name: str
    initial: str
    drifted: str
    expected: str
    benign: bool


def edit_cases(replacement: str = DEFAULT_REPLACEMENT) -> tuple[EditCase, ...]:
    initial = "header\ntarget = 1\ntail\n"
    return (
        EditCase("unchanged", initial, initial, f"header\n{replacement}tail\n", True),
        EditCase(
            "unique_prepend",
            initial,
            "notice\n" + initial,
            f"notice\nheader\n{replacement}tail\n",
            True,
        ),
        EditCase(
            "concurrent_target_change",
            initial,
            "header\ntarget = 9\ntail\n",
            "header\ntarget = 9\ntail\n",
            False,
        ),
        EditCase(
            "ambiguous_duplicate",
            initial,
            "notice\n" + initial + "target = 1\n",
            "notice\n" + initial + "target = 1\n",
            False,
        ),
    )


def _naive_coordinate_apply(text: str, replacement: str) -> str:
    pieces = text.splitlines(keepends=True)
    pieces[1:2] = [replacement]
    return "".join(pieces)


def _workspace(root: Path):
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return resolve_workspace(str(root))


def run_edit_replay(replacement: str = DEFAULT_REPLACEMENT) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ctx-oh-my-pi-edit-") as raw:
        base = Path(raw)
        old_state = os.environ.get("CTX_STATE_HOME")
        os.environ["CTX_STATE_HOME"] = str(base / "state")
        try:
            for index, case in enumerate(edit_cases(replacement)):
                root = base / f"case-{index}"
                root.mkdir()
                target = root / "fixture.txt"
                target.write_text(case.initial, encoding="utf-8")
                ws = _workspace(root)
                store = Store(ws.workspace_id)
                span = anchors.format_span(
                    2, 2, anchors.anchor(case.initial.splitlines()[1:2])
                )
                plan = create_edit_plan(
                    ws,
                    store,
                    {
                        "schema": REQUEST_SCHEMA,
                        "edits": [
                            {
                                "path": "fixture.txt",
                                "span": span,
                                "replacement": replacement,
                            }
                        ],
                    },
                )

                naive_text = _naive_coordinate_apply(case.drifted, replacement)
                target.write_text(case.drifted, encoding="utf-8")
                ctx_outcome = "applied"
                try:
                    receipt = apply_edit_plan(ws, plan)
                    ctx_text = target.read_text(encoding="utf-8")
                    relocated = bool(receipt["files"][0]["edits"][0]["relocated"])
                except EditTransactionError:
                    ctx_outcome = "refused"
                    ctx_text = target.read_text(encoding="utf-8")
                    relocated = False
                cases.append(
                    {
                        "case": case.name,
                        "class": "benign" if case.benign else "adversarial",
                        "naive": {
                            "complete_or_safe": naive_text == case.expected,
                            "outcome": "applied",
                        },
                        "ctx": {
                            "complete_or_safe": ctx_text == case.expected,
                            "outcome": ctx_outcome,
                            "relocated": relocated,
                        },
                    }
                )
        finally:
            if old_state is None:
                os.environ.pop("CTX_STATE_HOME", None)
            else:
                os.environ["CTX_STATE_HOME"] = old_state

    def rate(arm: str, class_name: str) -> float:
        selected = [item for item in cases if item["class"] == class_name]
        return sum(bool(item[arm]["complete_or_safe"]) for item in selected) / len(selected)

    naive_benign = rate("naive", "benign")
    ctx_benign = rate("ctx", "benign")
    naive_adversarial = rate("naive", "adversarial")
    ctx_adversarial = rate("ctx", "adversarial")
    return {
        "taskset": FROZEN_TASKSET,
        "evidence": "offline_production_path",
        "cases": cases,
        "metrics": {
            "benign_completion": {"naive": naive_benign, "ctx": ctx_benign},
            "adversarial_safety": {"naive": naive_adversarial, "ctx": ctx_adversarial},
            "benign_completion_percentage_point_change": 100 * (ctx_benign - naive_benign),
            "adversarial_safety_percentage_point_change": 100 * (
                ctx_adversarial - naive_adversarial
            ),
        },
    }


def _git(root: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "ctx-canary",
        "GIT_AUTHOR_EMAIL": "ctx-canary@example.invalid",
        "GIT_COMMITTER_NAME": "ctx-canary",
        "GIT_COMMITTER_EMAIL": "ctx-canary@example.invalid",
    }
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def _synthetic_hosts() -> list[hosts.DetectedHost]:
    def which(binary: str) -> str | None:
        return f"/usr/bin/{binary}" if binary in {"claude", "codex"} else None

    return [
        host
        for host in hosts.detect_all(which=which)
        if host.installed and host.harnessable
    ]


def _run_orchestration_arm(*, isolated: bool, worker_seconds: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ctx-oh-my-pi-orch-") as raw:
        root = Path(raw)
        _git(root, "init", "-q")
        (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
        (root / "a.txt").write_text("old a\n", encoding="utf-8")
        (root / "b.txt").write_text("old b\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "fixture")
        ws = resolve_workspace(str(root))
        cfg: OrchestratePolicy = replace(
            ws.config.orchestrate, isolated_worktrees=isolated
        )
        raw_plan = {
            "nodes": [
                {
                    "id": "a",
                    "goal": "edit a.txt",
                    "role": "implement",
                    "min_tier": "economy",
                    "deps": [],
                    "targets": ["a.txt"],
                },
                {
                    "id": "b",
                    "goal": "edit b.txt",
                    "role": "implement",
                    "min_tier": "economy",
                    "deps": [],
                    "targets": ["b.txt"],
                },
                {
                    "id": "verify",
                    "goal": "verify both",
                    "role": "verify",
                    "min_tier": "economy",
                    "deps": ["a", "b"],
                },
            ]
        }
        plan = build_route_plan("update two fixtures", raw_plan, _synthetic_hosts(), cfg)

        def launch(host, work_root, prompt, exe, *, timeout, model=""):
            if "node 'a'" in prompt:
                time.sleep(worker_seconds)
                (work_root / "a.txt").write_text("new a\n", encoding="utf-8")
            elif "node 'b'" in prompt:
                time.sleep(worker_seconds)
                (work_root / "b.txt").write_text("new b\n", encoding="utf-8")
            return 0, "worker completed", ""

        started = time.perf_counter()
        result = run_route(ws, plan, cfg, launch=launch)
        duration = time.perf_counter() - started
        success = (
            (root / "a.txt").read_text(encoding="utf-8") == "new a\n"
            and (root / "b.txt").read_text(encoding="utf-8") == "new b\n"
            and all(outcome.status == "ok" for outcome in result.outcomes)
        )
        return {
            "success": success,
            "duration_seconds": round(duration, 6),
            "wave_policies": list(result.wave_policies),
            "mutation_isolation": [
                outcome.isolation for outcome in result.outcomes if outcome.node_id in {"a", "b"}
            ],
        }


def run_orchestration_replay(worker_seconds: float = 0.35) -> dict[str, Any]:
    serial = _run_orchestration_arm(isolated=False, worker_seconds=worker_seconds)
    isolated = _run_orchestration_arm(isolated=True, worker_seconds=worker_seconds)
    speedup = serial["duration_seconds"] / isolated["duration_seconds"]
    return {
        "evidence": "local_simulation_production_path",
        "worker_seconds": worker_seconds,
        "serial": serial,
        "isolated": isolated,
        "observed_wall_time_speedup": round(speedup, 4),
        "ideal_worker_makespan_speedup": 2.0,
    }


def _json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def run_live_proposals(
    selected_hosts: tuple[str, ...], *, timeout: float, max_calls: int
) -> list[dict[str, Any]]:
    installed = {host.spec.name: host for host in hosts.installed_harnessable()}
    prompt = (
        "Return only a compact JSON object with one key named replacement. "
        "The existing exact source line is `target = 1`. Change its integer "
        "value to 2. The replacement must be the complete line including a "
        "trailing newline encoded as JSON. Do not use tools or markdown."
    )
    records: list[dict[str, Any]] = []
    calls = 0
    with tempfile.TemporaryDirectory(prefix="ctx-oh-my-pi-live-") as raw:
        root = Path(raw)
        _git(root, "init", "-q")
        (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
        _git(root, "add", "ctx.toml")
        _git(root, "commit", "-qm", "canary fixture")
        for name in selected_hosts:
            if calls >= max_calls:
                records.append({"host": name, "execution_status": "skipped_call_cap"})
                continue
            host = installed.get(name)
            if host is None or not host.spec.unattended:
                records.append({"host": name, "execution_status": "skipped_unavailable"})
                continue
            calls += 1
            started = time.perf_counter()
            code, output, stderr, usage = _launch_host(
                host, root, prompt, "ctx", timeout=timeout, model=""
            )
            doc = _json_object(output)
            replacement = doc.get("replacement") if isinstance(doc, dict) else None
            valid = replacement == DEFAULT_REPLACEMENT
            record: dict[str, Any] = {
                "host": name,
                "model": host.model,
                "execution_status": "live",
                "exit_code": code,
                "duration_seconds": round(time.perf_counter() - started, 6),
                "proposal_valid": valid,
                "stderr_present": bool(stderr),
                "usage": usage.as_dict() if usage is not None else None,
            }
            if valid:
                record["matched_replay"] = run_edit_replay(replacement)["metrics"]
            records.append(record)
    return records


def evaluate(
    *, live_hosts: tuple[str, ...] = (), timeout: float = 120.0, max_live_calls: int = 2
) -> dict[str, Any]:
    started = time.time()
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "taskset": FROZEN_TASKSET,
        "started_at_unix": started,
        "edit": run_edit_replay(),
        "orchestration": run_orchestration_replay(),
        "live": [],
    }
    if live_hosts:
        record["live"] = run_live_proposals(
            live_hosts, timeout=timeout, max_calls=max_live_calls
        )
    else:
        record["live"] = [{"execution_status": "skipped_not_requested"}]
    record["duration_seconds"] = round(time.time() - started, 6)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-host",
        action="append",
        choices=("claude", "codex"),
        default=[],
        help="run one bounded live proposal on this installed host (repeatable)",
    )
    parser.add_argument("--max-live-calls", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.max_live_calls < 0:
        parser.error("--max-live-calls must be non-negative")
    record = evaluate(
        live_hosts=tuple(args.live_host),
        timeout=args.timeout,
        max_live_calls=args.max_live_calls,
    )
    rendered = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
