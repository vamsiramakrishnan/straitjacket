"""Multi-harness collaboration plane for ctx.

Codex, Claude Code, Gemini CLI, and Antigravity all live on this machine.
This module lets them hand bounded tasks to each other through one
append-only ledger in the workspace (``.ctx/collab/ledger.jsonl``), without an
external control plane:

- **Tasks** carry a goal, explicit ownership leases (paths owned and paths
  that must not be touched), a verification command, and context files.
- **Claims** are leases: exactly one harness holds a task at a time; a second
  claim is refused until a completion or an explicit release lands.
- **Completions** are structured packets — status ``done``/``blocked``/
  ``failed``, summary, files changed, the verification actually run, and
  follow-ups. Console output is transport; the packet is the record.
- **Reviews** record a reviewer harness and an approve/reject verdict with
  evidence, so winners can be cross-checked by a different harness than the
  one that produced them.
- **Integration** is the gate: only an accepted integration closes a task.

Every row is a typed envelope under ``ctx.collab/v1``. The ledger is plain
JSONL so it survives crashes, is trivially diffable, and stays readable by any
harness without importing this module.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = "ctx.collab/v1"

# Headless invocation per harness CLI. All four accept a print-mode prompt;
# codex uses the `exec` subcommand instead of a flag.
HARNESSES: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "-p"),
    "codex": ("codex", "exec"),
    "gemini": ("gemini", "-p"),
    "agy": ("agy", "-p"),
}


class CollabError(Exception):
    """Raised for lease conflicts and malformed ledger operations."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _task_id(goal: str, seq: int) -> str:
    digest = hashlib.sha1(f"{seq}:{goal}".encode("utf-8")).hexdigest()[:6]
    return f"t-{seq:03d}-{digest}"


class Ledger:
    """Append-only event log folded into per-task state."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root)
        self.path = self.dir / "ledger.jsonl"

    # ------------------------------------------------------------- events
    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final write must not kill the ledger
        return rows

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        rows = self.load()
        event = {
            "schema": SCHEMA,
            "kind": kind,
            "ts": _now(),
            "seq": len(rows) + 1,
            **fields,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=False) + "\n")
        return event

    # ------------------------------------------------------------ folding
    def state(self) -> dict[str, dict[str, Any]]:
        """Latest-writer-wins view per task."""
        tasks: dict[str, dict[str, Any]] = {}
        for row in self.load():
            kind = row.get("kind")
            tid = row.get("task_id")
            if kind == "task":
                tasks[tid] = {**row, "status": "open", "holder": None}
            elif kind == "claim":
                cur = tasks.setdefault(tid, {"task_id": tid})
                cur["holder"] = row.get("by")
                cur["status"] = "claimed"
            elif kind == "completion":
                cur = tasks.setdefault(tid, {"task_id": tid})
                cur["status"] = row.get("completion_status", "unknown")
                cur["holder"] = None
                cur["last_completion"] = row
            elif kind == "review":
                cur = tasks.setdefault(tid, {"task_id": tid})
                cur.setdefault("reviews", []).append(row)
            elif kind == "integration":
                cur = tasks.setdefault(tid, {"task_id": tid})
                cur["status"] = (
                    "integrated" if row.get("accepted") else "rejected"
                )
        return tasks

    # ------------------------------------------------------------ actions
    def create_task(
        self,
        goal: str,
        *,
        owns: tuple[str, ...] = (),
        avoid: tuple[str, ...] = (),
        verify: str = "",
        context: tuple[str, ...] = (),
        created_by: str = "coordinator",
        permission: str = "default",
    ) -> str:
        seq = sum(1 for r in self.load() if r.get("kind") == "task") + 1
        tid = _task_id(goal, seq)
        self.append(
            "task",
            task_id=tid,
            goal=goal,
            owns=list(owns),
            avoid=list(avoid),
            verify=verify,
            context=list(context),
            created_by=created_by,
            permission=permission,
        )
        return tid

    def claim(self, task_id: str, by: str) -> dict[str, Any]:
        tasks = self.state()
        task = tasks.get(task_id)
        if task is None:
            raise CollabError(f"unknown task {task_id}")
        holder = task.get("holder")
        if holder and holder != by:
            raise CollabError(
                f"{task_id} is leased to {holder}; wait for its completion"
            )
        if task.get("status") == "integrated":
            raise CollabError(f"{task_id} is already integrated")
        return self.append("claim", task_id=task_id, by=by)

    def complete(
        self,
        task_id: str,
        by: str,
        *,
        status: str,
        summary: str,
        files_changed: tuple[str, ...] = (),
        verification: str = "",
        followups: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if status not in ("done", "blocked", "failed"):
            raise CollabError("status must be done, blocked, or failed")
        return self.append(
            "completion",
            task_id=task_id,
            by=by,
            completion_status=status,
            summary=summary,
            files_changed=list(files_changed),
            verification=verification,
            followups=list(followups),
        )

    def review(
        self,
        task_id: str,
        reviewer: str,
        *,
        verdict: str,
        evidence: str = "",
    ) -> dict[str, Any]:
        if verdict not in ("approve", "reject"):
            raise CollabError("verdict must be approve or reject")
        return self.append(
            "review",
            task_id=task_id,
            reviewer=reviewer,
            verdict=verdict,
            evidence=evidence,
        )

    def integrate(self, task_id: str, *, accepted: bool, note: str) -> dict[str, Any]:
        return self.append(
            "integration", task_id=task_id, accepted=accepted, note=note
        )


def handoff_packet(task: dict[str, Any], to_harness: str) -> str:
    """Render the worker instruction a receiving harness acts on."""
    lines = [
        f"# Handoff {task.get('task_id')} -> {to_harness}",
        f"Goal: {task.get('goal', '')}",
        f"You own exactly these paths: {', '.join(task.get('owns') or ()) or '(none declared)'}",
        f"Do NOT touch: {', '.join(task.get('avoid') or ()) or '(nothing extra excluded)'}",
        f"Permission mode: {task.get('permission', 'default')}",
    ]
    if task.get("context"):
        lines.append("Read first: " + ", ".join(task["context"]))
    lines.append(
        "You are not alone in this repository; never revert other harnesses' work."
    )
    if task.get("verify"):
        lines.append(f"Verify with: {task['verify']}")
    lines += [
        "Finish by reporting a completion packet:",
        "  status: done | blocked | failed",
        "  summary: what you did",
        "  files_changed: comma-separated paths or none",
        "  verification: command run and its result",
        "  followups: anything the next harness or human needs",
    ]
    return "\n".join(lines)


def spawn(
    harness: str,
    prompt: str,
    *,
    timeout: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """Run one harness headlessly in print mode and return its result."""
    argv = HARNESSES.get(harness)
    if argv is None:
        raise CollabError(
            f"unknown harness {harness!r}; known: {', '.join(sorted(HARNESSES))}"
        )
    return runner([*argv, prompt], capture_output=True, text=True, timeout=timeout)


def ledger_for_workspace(ws: Any) -> Ledger:
    return Ledger(Path(ws.root) / ".ctx" / "collab")
