"""Durable agy sessions: native stream events, wakeups, and process watchdogs.

Transport completion never completes or integrates a collaboration task.
"""
from __future__ import annotations

import fcntl
import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

TERMINAL = {"completed", "blocked", "failed", "timed_out", "cancelled", "orphaned"}


def _write(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _event(root: Path, state: dict, kind: str, **data) -> None:
    state["seq"] = state.get("seq", 0) + 1
    state["updated_at"] = time.time()
    event = {"seq": state["seq"], "kind": kind, "time": state["updated_at"], **data}
    with (root / "events.jsonl").open("a") as handle:
        handle.write(json.dumps(event) + "\n")
        handle.flush()
    _write(root / "state.json", state)
    try:
        fd = os.open(root / "wake", os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"!")
        finally:
            os.close(fd)
    except OSError:
        pass  # Durable events and the watchdog cover absent/slow consumers.


def launch(workspace: Path, task_id: str, prompt: str, *, model: str,
           mode: str = "plan", conversation: str = "", timeout: float = 300,
           session_root: Path | None = None) -> dict:
    if mode not in {"plan", "accept-edits"} or not 0 < timeout <= 1800 or not model:
        raise ValueError("explicit model, supported mode and 0 < timeout <= 1800 required")
    workspace = Path(workspace).resolve()
    root = (session_root or workspace / ".ctx/collab/sessions") / uuid.uuid4().hex
    root.mkdir(parents=True, mode=0o700)
    os.mkfifo(root / "wake", 0o600)
    request = dict(workspace=str(workspace), task_id=task_id, prompt=prompt,
                   model=model, mode=mode, conversation=conversation, timeout=timeout)
    _write(root / "request.json", request)
    state = {"session_id": root.name, "task_id": task_id, "status": "queued",
             "model": model, "conversation_id": conversation or None,
             "seq": 0, "created_at": time.time(), "task_completed": False}
    _write(root / "state.json", state)
    try:
        with (root / "supervisor.log").open("ab") as log:
            supervisor = subprocess.Popen([sys.executable, "-m", "ctx.collab_sessions", str(root)],
                             cwd=workspace, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=log, start_new_session=True)
            (root / "supervisor.pid").write_text(str(supervisor.pid))
    except OSError as exc:
        state.update(status="failed", error=str(exc))
        _event(root, state, "terminal", status="failed")
        raise
    return {**state, "directory": str(root)}


def _native_result(row: dict) -> dict | None:
    if row.get("event") == "result" and isinstance(row.get("result"), dict):
        return row["result"]
    if row.get("type") == "result" or ("status" in row and "conversation_id" in row):
        return row
    return None


def result_status(result: dict | None, returncode: int) -> str:
    if returncode:
        return "failed"
    if not result or result.get("denied_actions") or result.get("is_error"):
        return "blocked"
    response = result.get("response", result.get("result", ""))
    return "completed" if response and result.get("status", "SUCCESS") == "SUCCESS" else "blocked"


def recover(root: Path) -> dict:
    """Reconcile a finished transport after a parser upgrade, without another call."""
    with (root / "recover.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = status(root)
        if state["status"] != "blocked" or state.get("returncode") != 0:
            return state
        result = None
        with (root / "stdout.jsonl").open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and _native_result(row):
                    result = _native_result(row)
        if result and result.get("conversation_id") == state.get("conversation_id"):
            _write(root / "result.json", result)
            (root / "response.md").write_text(result.get("response", ""))
            state.update(status=result_status(result, 0), usage=result.get("usage", {}))
            if state["status"] == "completed":
                _event(root, state, "reconciled", status=state["status"])
        return state


def worker(root: Path) -> None:
    request = json.loads((root / "request.json").read_text())
    state = json.loads((root / "state.json").read_text())
    state.update(status="running", supervisor_pid=os.getpid())
    _event(root, state, "started")
    lock = None
    proc = None
    result = None
    reason = None
    last_update = None
    try:
        if request["conversation"]:
            key = hashlib.sha256(request["conversation"].encode()).hexdigest()
            lock = (root.parent / f"conversation-{key}.lock").open("a")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        argv = ["agy", "--model", request["model"], "--mode", request["mode"],
                "--output-format", "stream-json"]
        if request["conversation"]:
            argv += ["--conversation", request["conversation"]]
        argv += ["-p", request["prompt"]]
        proc = subprocess.Popen(argv, cwd=request["workspace"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        state["pid"] = proc.pid
        _event(root, state, "process_started")
        deadline = time.monotonic() + request["timeout"]
        buffer = b""
        with selectors.DefaultSelector() as selector, (root / "stdout.jsonl").open("wb") as out, (root / "stderr.txt").open("wb") as err:
            selector.register(proc.stdout, selectors.EVENT_READ, out)
            selector.register(proc.stderr, selectors.EVENT_READ, err)
            while selector.get_map():
                if (root / "cancel").exists() or time.monotonic() >= deadline:
                    reason = "cancelled" if (root / "cancel").exists() else "timed_out"
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGTERM)
                    break
                for key, _ in selector.select(timeout=0.5):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.write(chunk)
                    key.data.flush()
                    if key.data is out:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            try:
                                row = json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                            if not isinstance(row, dict):
                                continue
                            conversation = row.get("conversation_id") or row.get("session_id")
                            if row.get("event") == "init":
                                init = row.get("init", {})
                                if init.get("cwd") and Path(init["cwd"]).resolve() != Path(request["workspace"]).resolve():
                                    raise ValueError("agy resumed a different workspace")
                                if init.get("model") and init["model"] != request["model"]:
                                    raise ValueError("agy used a different model")
                            if conversation:
                                if request["conversation"] and conversation != request["conversation"]:
                                    raise ValueError("agy resumed a different conversation")
                                state["conversation_id"] = conversation
                            native = _native_result(row)
                            if native is not None:
                                native_id = native.get("conversation_id") or native.get("session_id")
                                if native_id != state.get("conversation_id"):
                                    raise ValueError("result belongs to a different conversation")
                                result = native
                                _write(root / "result.json", native)
                                (root / "response.md").write_text(native.get("response", native.get("result", "")))
                            step = row.get("step_update", {})
                            update = (row.get("event", row.get("type")), step.get("step_index"), step.get("state"))
                            if native or update != last_update:
                                _event(root, state, "result_received" if native else "native_update",
                                       type=update[0])
                                last_update = update
                        if len(buffer) > 1024 * 1024:
                            buffer = b""  # Raw output remains intact on disk.
                if proc.poll() is not None and not selector.get_map():
                    break
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            returncode = proc.wait()
        state["returncode"] = returncode
        if reason:
            state["status"] = reason
        else:
            state["status"] = result_status(result, returncode)
        if result:
            state["usage"] = result.get("usage", {})
    except Exception as exc:
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                stream.close()
        if lock is not None:
            lock.close()
        _event(root, state, "terminal", status=state["status"])


def status(root: Path) -> dict:
    state = json.loads((root / "state.json").read_text())
    pid = state.get("supervisor_pid")
    if not pid and (root / "supervisor.pid").exists():
        pid = int((root / "supervisor.pid").read_text())
    if state["status"] not in TERMINAL and pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            state = {**state, "status": "orphaned"}
    return state


def watch(roots: list[Path], *, until: str = "any", timeout: float = 30) -> dict:
    if until not in {"any", "all"} or not roots or not 0 <= timeout <= 60:
        raise ValueError("watch requires sessions, any/all, and a wait of 0 to 60 seconds")
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        handles = []
        try:
            for root in roots:
                fd = os.open(root / "wake", os.O_RDWR | os.O_NONBLOCK)
                handles.append(fd)
                selector.register(fd, selectors.EVENT_READ)
            while True:
                states = [status(root) for root in roots]
                done = [s for s in states if s["status"] in TERMINAL]
                satisfied = bool(done) if until == "any" else len(done) == len(states)
                remaining = deadline - time.monotonic()
                if satisfied or remaining <= 0:
                    return {"satisfied": satisfied, "until": until, "sessions": states}
                # FIFO wakeup wins; a five-second watchdog covers lost notifications.
                for key, _ in selector.select(timeout=min(5, remaining)):
                    os.read(key.fd, 65536)
        finally:
            for fd in handles:
                os.close(fd)


if __name__ == "__main__":
    worker(Path(sys.argv[1]))
