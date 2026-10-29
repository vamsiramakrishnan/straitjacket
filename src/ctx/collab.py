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
import fcntl
import functools
import json
import os
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


def _locked(method):
    @functools.wraps(method)
    def transaction(self, *args, **kwargs):
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "ledger.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return method(self, *args, **kwargs)
    return transaction


def _path(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise CollabError("ownership paths must be relative without '..'")
    return path.as_posix().rstrip("/")


def _under(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


def workspace_fingerprint(root: Path, owns: list[str]) -> str:
    digest = hashlib.sha256()
    paths = set()
    for name in owns:
        path = root / _path(name)
        if not path.resolve().is_relative_to(root.resolve()):
            raise CollabError("owned path resolves outside workspace")
        paths.update(path.rglob("*") if path.is_dir() else [path])
    for path in sorted(paths):
        if any(p in {".git", ".ctx", "__pycache__", ".pytest_cache"} for p in path.relative_to(root).parts):
            continue
        if path.is_dir():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_symlink():
            digest.update(b"symlink:" + os.readlink(path).encode())
        elif path.is_file():
            with path.open("rb") as handle:
                while chunk := handle.read(65536):
                    digest.update(chunk)
        else:
            digest.update(b"missing")
    return digest.hexdigest()


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
        raw = self.path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        for index, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not raw.endswith("\n"):
                    continue
                raise CollabError("corrupt ledger row; refusing to discard history")
        return rows

    @_locked
    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        return self._append(kind, **fields)

    def _append(self, kind: str, **fields: Any) -> dict[str, Any]:
        rows = self.load()
        event = {
            "schema": SCHEMA,
            "kind": kind,
            "ts": _now(),
            "seq": len(rows) + 1,
            **fields,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raw = self.path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                tail = raw.rsplit(b"\n", 1)[-1]
                try:
                    json.loads(tail)
                except (ValueError, UnicodeError):
                    (self.dir / f"torn-{hashlib.sha256(tail).hexdigest()[:16]}.json").write_bytes(tail)
                    self.path.write_bytes(raw[:-len(tail)])
                else:
                    with self.path.open("ab") as handle:
                        handle.write(b"\n")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
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
                cur["reviews"] = []
                cur.pop("last_completion", None)
                cur.pop("verification", None)
            elif kind == "completion":
                cur = tasks.setdefault(tid, {"task_id": tid})
                cur["status"] = row.get("completion_status", "unknown")
                cur["holder"] = None
                cur["last_completion"] = row
            elif kind == "review":
                cur = tasks.setdefault(tid, {"task_id": tid})
                cur.setdefault("reviews", []).append(row)
            elif kind == "plan":
                tasks[tid]["plan"] = row["steps"]
            elif kind == "progress":
                tasks[tid].setdefault("progress", {})[row["step"]] = row
            elif kind == "verification":
                tasks[tid]["verification"] = row
            elif kind == "session":
                tasks[tid].setdefault("sessions", []).append(row["session_id"])
            elif kind == "integration":
                cur = tasks.setdefault(tid, {"task_id": tid})
                cur["status"] = (
                    "integrated" if row.get("accepted") else "rejected"
                )
        return tasks

    # ------------------------------------------------------------ actions
    @_locked
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
        for path in (*owns, *avoid):
            _path(path)
        self._append(
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

    @_locked
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
        if not by.strip():
            raise CollabError("holder must be named")
        for other_id, other in tasks.items():
            if other_id == task_id or not other.get("holder"):
                continue
            if any(_under(_path(a), _path(b)) or _under(_path(b), _path(a))
                   for a in task.get("owns", []) for b in other.get("owns", [])):
                raise CollabError(f"ownership overlaps active task {other_id}")
        if holder == by:
            return task
        return self._append("claim", task_id=task_id, by=by)

    @_locked
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
        task = self.state().get(task_id)
        if not task or task.get("holder") != by:
            raise CollabError("completion requires the active lease holder")
        from ctx.collab_sessions import status as session_status, TERMINAL
        if any(session_status(self.dir / "sessions" / sid)["status"] not in TERMINAL
               for sid in task.get("sessions", [])):
            raise CollabError("wait for or cancel the active session before completion")
        for value in files_changed:
            path = _path(value)
            if (not any(_under(path, _path(root)) for root in task.get("owns", []))
                    or any(_under(path, _path(root)) for root in task.get("avoid", []))):
                raise CollabError(f"changed path outside ownership: {path}")
        if status == "done" and task.get("plan"):
            progress = task.get("progress", {})
            if any(progress.get(str(i), {}).get("status") != "done" for i in range(len(task["plan"]))):
                raise CollabError("all planned steps require completion evidence")
        return self._append(
            "completion",
            task_id=task_id,
            by=by,
            completion_status=status,
            summary=summary,
            files_changed=list(files_changed),
            verification=verification,
            followups=list(followups),
        )

    @_locked
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
        task = self.state().get(task_id)
        if not task or task.get("status") != "done":
            raise CollabError("review requires a completed task")
        if reviewer == task["last_completion"]["by"] or not evidence.strip():
            raise CollabError("independent reviewer and evidence required")
        return self._append(
            "review",
            task_id=task_id,
            reviewer=reviewer,
            verdict=verdict,
            evidence=evidence,
            fingerprint=workspace_fingerprint(self.dir.parent.parent, task.get("owns", [])),
        )

    @_locked
    def integrate(self, task_id: str, *, accepted: bool, note: str) -> dict[str, Any]:
        task = self.state().get(task_id)
        if not task:
            raise CollabError("unknown task")
        if accepted:
            verdicts = {r["reviewer"]: r["verdict"] for r in task.get("reviews", [])}
            verification = task.get("verification", {})
            fingerprint = workspace_fingerprint(self.dir.parent.parent, task.get("owns", []))
            if (task.get("status") != "done" or not verdicts or "reject" in verdicts.values()
                    or verification.get("returncode") != 0
                    or verification.get("completion_seq") != task.get("last_completion", {}).get("seq")
                    or verification.get("fingerprint_before") != fingerprint
                    or verification.get("fingerprint_after") != fingerprint
                    or any(r.get("fingerprint") != fingerprint for r in task.get("reviews", []))):
                raise CollabError("integration requires current passing verification and independent approval")
        return self._append(
            "integration", task_id=task_id, accepted=accepted, note=note
        )

    @_locked
    def plan(self, task_id: str, by: str, steps: list[dict]) -> None:
        task = self.state().get(task_id, {})
        if task.get("holder") != by:
            raise CollabError("plan requires active lease holder")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise CollabError("plan must contain 1 to 8 steps")
        if any(not isinstance(s, dict) or not isinstance(s.get("goal"), str)
               or not s["goal"].strip() or not isinstance(s.get("check"), str)
               or not s["check"].strip() for s in steps):
            raise CollabError("each step needs a goal and validation check")
        if task.get("plan"):
            raise CollabError("task already has a plan; create a follow-up task to replan")
        self._append("plan", task_id=task_id, by=by, steps=steps)

    @_locked
    def progress(self, task_id: str, by: str, step: int, status: str, evidence: str) -> None:
        task = self.state().get(task_id, {})
        if task.get("holder") != by or not 0 <= step < len(task.get("plan", [])):
            raise CollabError("progress requires a valid step and active holder")
        if status not in {"done", "blocked"} or not evidence.strip():
            raise CollabError("progress requires done/blocked status and evidence")
        self._append("progress", task_id=task_id, by=by, step=str(step), status=status, evidence=evidence)

    def verify(self, task_id: str, cwd: Path, runner=subprocess.run) -> dict:
        import shlex
        task = self.state().get(task_id, {})
        if task.get("status") != "done" or not task.get("verify"):
            raise CollabError("verification requires a completed task and command")
        cwd = Path(cwd).resolve()
        if cwd != self.dir.parent.parent.resolve():
            raise CollabError("verification must run in the task workspace")
        before = workspace_fingerprint(cwd, task.get("owns", []))
        try:
            proc = runner(shlex.split(task["verify"]), cwd=cwd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
            proc = subprocess.CompletedProcess(task['verify'], 124,
                stdout=decoded(exc.stdout), stderr=decoded(exc.stderr) + '\nverification timed out')
        outputs = {}
        for stream in ("stdout", "stderr"):
            data = (getattr(proc, stream) or "").encode()
            path = self.dir / f"verify-{hashlib.sha256(data).hexdigest()}.txt"
            path.write_bytes(data)
            outputs[stream + "_file"] = str(path)
        return self.append("verification", task_id=task_id,
                           completion_seq=task["last_completion"]["seq"], command=task["verify"],
                           returncode=proc.returncode, fingerprint_before=before,
                           fingerprint_after=workspace_fingerprint(cwd, task.get("owns", [])), **outputs)

    @_locked
    def launch_session(self, task_id: str, prompt: str, **options) -> dict:
        from ctx.collab_sessions import launch, status, TERMINAL
        task = self.state().get(task_id, {})
        if task.get("holder") != "agy":
            raise CollabError("agy must hold the task lease")
        if options.get('mode') == 'accept-edits':
            raise CollabError('use prewalk for editing in an isolated worktree')
        for session in task.get("sessions", []):
            if status(self.dir / "sessions" / session)["status"] not in TERMINAL:
                raise CollabError(f"task already has active session {session}")
        result = launch(self.dir.parent.parent, task_id, prompt, **options)
        self._append("session", task_id=task_id, session_id=result["session_id"])
        return result

    @_locked
    def start_prewalk(self, task_id: str, **options) -> dict:
        from ctx.native_prewalk import _start
        from ctx.collab_sessions import status, TERMINAL
        task = self.state().get(task_id, {})
        if any(status(self.dir / 'sessions' / sid)['status'] not in TERMINAL
               for sid in task.get('sessions', [])):
            raise CollabError('task already has an active session')
        return _start(self, task_id, **options)

    @_locked
    def continue_prewalk(self, guide_id: str, **options) -> dict:
        from ctx.native_prewalk import _continue_session
        from ctx.collab_sessions import status, TERMINAL
        if len(guide_id) != 32 or any(c not in '0123456789abcdef' for c in guide_id):
            raise CollabError('invalid guide session ID')
        meta = json.loads((self.dir / 'sessions' / guide_id / 'prewalk.json').read_text())
        task = self.state().get(meta['task_id'], {})
        if any(status(self.dir / 'sessions' / sid)['status'] not in TERMINAL
               for sid in task.get('sessions', [])):
            raise CollabError('task already has an active session')
        return _continue_session(self, guide_id, **options)


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
    if task.get("plan"):
        lines.append("Task steps (preserve these validation requirements):")
        for index, step in enumerate(task["plan"]):
            state = task.get("progress", {}).get(str(index), {})
            lines.append(f"  {index}: {step['goal']} | check: {step['check']} | "
                         f"status: {state.get('status', 'pending')} | evidence: {state.get('evidence', '')}")
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
    model: str = "",
    mode: str = "",
    cwd: str | Path | None = None,
    conversation: str = "",
    structured: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """Run one harness headlessly in print mode and return its result."""
    argv = HARNESSES.get(harness)
    if argv is None:
        raise CollabError(
            f"unknown harness {harness!r}; known: {', '.join(sorted(HARNESSES))}"
        )
    command = list(argv[:-1] if argv[-1] == "-p" else argv)
    if model:
        command += ["--model", model]
    if mode:
        if harness != "agy" or mode not in {"plan", "accept-edits"}:
            raise CollabError("execution mode requires agy plan or accept-edits")
        command += ["--mode", mode]
    if conversation or structured:
        if harness != "agy":
            raise CollabError("native conversation continuation currently supports agy only")
        command += ["--output-format", "json"]
        if conversation:
            command += ["--conversation", conversation]
    kwargs = {"capture_output": True, "text": True, "timeout": timeout}
    if cwd is not None:
        kwargs["cwd"] = cwd
    return runner([*command, *(["-p"] if argv[-1] == "-p" else []), prompt], **kwargs)


def ledger_for_workspace(ws: Any) -> Ledger:
    return Ledger(Path(ws.root) / ".ctx" / "collab")
