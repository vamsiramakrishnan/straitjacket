"""Async tool boundary for any embedding harness; no provider SDK dependency.

Every worker owns its Store connection. Process cancellation is cooperative
at capture, shielded until the owned process group is stopped and partial
evidence has been published. No process-global cwd or environment mutation.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import math
import os
from pathlib import Path
import re
import threading
import time

from ctx.execution import run_capture
from ctx.store import Store
from ctx.toolresult import CommandResult, render_command_result
from ctx.tooltelemetry import record_tool_call
from ctx.workspace import resolve_workspace

READ_OPS = frozenset({"get", "search", "stats", "map", "def", "refs", "diag",
                     "callers", "callees", "impact", "diff", "q", "repo"})


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return float(value)


class ToolSession:
    """A workspace-scoped tool session sharing the CLI's capture and retrieval.

    Commands retain the caller's OS authority. Workspace scope constrains cwd
    and retrieval, not what an executable can do. env is an explicit child-only
    overlay; values are never written to manifests or telemetry.
    """

    def __init__(self, workspace, *, timeout=600, max_tokens=None, env=None, producer="sdk"):
        self.root = resolve_workspace(str(workspace)).root
        self.timeout = _timeout(timeout)
        if max_tokens is not None and (type(max_tokens) is not int or not 64 <= max_tokens <= 4000):
            raise ValueError("max_tokens must be an integer in 64..4000")
        self.max_tokens = max_tokens
        self.env = dict(env or {})
        self.producer = producer
        self._refs: set[str] = set()
        self._lock = threading.Lock()

    @property
    def handles(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._refs))

    def _admit(self, text):
        with self._lock:
            self._refs.update(m.group(0) for m in re.finditer(r"\b(?:run|blob|snapshot):[0-9a-f]{6,64}\b", text))

    def _workspace(self):
        # Refresh gitHead and committed policy after edits/commits. Keep budgets
        # on a per-call copy; parallel tools cannot loosen one another's policy.
        ws = resolve_workspace(str(self.root))
        if self.max_tokens is not None:
            b = ws.config.budgets
            ws = replace(ws, config=replace(ws.config, budgets=replace(b,
                result_tokens=min(b.result_tokens, self.max_tokens),
                digest_tokens=min(b.digest_tokens, self.max_tokens))))
        return ws

    async def _worker(self, function, *args):
        stop = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(function, stop, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            # Repeated caller cancellation must not detach the cleanup worker.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()  # consume a cleanup error; cancellation wins
            raise

    async def run(self, command, *, working_directory="", timeout_seconds=None,
                  focus=None, delivery="auto") -> CommandResult:
        """Execute argv, or an explicit Bash command string, with typed status."""
        timeout = self.timeout if timeout_seconds is None else _timeout(timeout_seconds)
        if timeout > self.timeout:
            raise ValueError("command timeout exceeds the session timeout")
        if isinstance(command, str) and not command.strip():
            raise ValueError("command must not be empty")
        argv = ["bash", "-lc", command] if isinstance(command, str) else list(command)
        if not argv or not all(isinstance(a, str) for a in argv) or not argv[0] or "\x00" in "".join(argv):
            raise ValueError("command needs nonempty argv or a shell string")
        if delivery not in {"auto", "digest"}:
            raise ValueError("delivery must be auto or digest")
        return await self._worker(self._run, argv, working_directory, timeout, focus, delivery)

    def _run(self, stop, argv, cwd, timeout, focus, delivery):
        from ctx.retrieval import record_telemetry
        started = time.monotonic()
        if stop.is_set():
            from ctx.execution import ExecutionError
            raise ExecutionError("command cancelled before launch")
        ws = self._workspace()
        workdir = ws.confine(cwd or ".", must_exist=True)
        if not workdir.is_dir():
            raise ValueError("working_directory must be a directory")
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        try:
            capture = run_capture(ws, argv, cwd=ws.relativize(workdir) or ".", timeout=timeout,
                store=store, cancelled=stop.is_set, env={**os.environ, **self.env})
            result = render_command_result(ws, store, capture, focus=focus, delivery=delivery)
            self._admit(result.ref)
            delivered = len(result.to_json().encode())
            raw = sum(s["bytes"] for s in capture.manifest["streams"].values())
            record_telemetry(store, "run", raw, delivered)
            record_tool_call(store, op="run", delivered_bytes=delivered,
                elapsed_seconds=time.monotonic()-started, captured_bytes=raw, result=result,
                producer=self.producer)
            return result
        except Exception as exc:
            record_tool_call(store, op="run", delivered_bytes=0, elapsed_seconds=time.monotonic()-started,
                error=type(exc).__name__, producer=self.producer)
            raise
        finally:
            store.close()

    async def retrieve(self, op, *, ref=None, patterns=None, selector=None, options=None) -> str:
        """Typed observe-only retrieval; shares the MCP's bounded dispatch."""
        if op not in READ_OPS:
            raise ValueError("operation is not a read operation")
        from ctx.refs import parse_ref
        if ref is not None and parse_ref(ref).workspace_alias:
            raise ValueError("cross-workspace retrieval is outside this session")
        if options and "workspace" in options:
            raise ValueError("workspace overrides are outside this session")
        for key in ("refA", "refB", "target"):
            if options and isinstance(options.get(key), str) and options[key].startswith("ws:"):
                raise ValueError("cross-workspace retrieval is outside this session")
        if patterns is not None and (not isinstance(patterns, (list, tuple)) or not 1 <= len(patterns) <= 16 or not all(isinstance(p, str) for p in patterns)):
            raise ValueError("patterns must contain 1..16 strings")
        return await self._worker(self._retrieve, {"op": op, "ref": ref,
            "patterns": list(patterns or []), "selector": dict(selector or {}), "options": dict(options or {})})

    def _retrieve(self, stop, args):
        from ctx.mcp import dispatch_bounded
        started = time.monotonic()
        ws = self._workspace()
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        try:
            if stop.is_set():
                return ""
            # No workspace override, task/send or execute operations admitted.
            text = dispatch_bounded(ws, store, args)
            self._admit(text)
            record_tool_call(store, op=args["op"], delivered_bytes=len(text.encode("utf-8", "surrogateescape")),
                elapsed_seconds=time.monotonic()-started, producer=self.producer)
            return text
        except Exception as exc:
            record_tool_call(store, op=args["op"], delivered_bytes=0, elapsed_seconds=time.monotonic()-started,
                error=type(exc).__name__, producer=self.producer)
            raise
        finally:
            store.close()

    async def get(self, ref, *, start_line, end_line):
        if type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line:
            raise ValueError("get needs an inclusive positive line range")
        return await self.retrieve("get", ref=ref, selector={"lines": f"{start_line}:{end_line}"})

    async def search(self, ref, pattern, *, context=3, fixed=False):
        if type(context) is not int or not 0 <= context <= 100:
            raise ValueError("context must be an integer in 0..100")
        return await self.retrieve("search", ref=ref, patterns=[pattern], options={"context": context, "fixed": fixed})

    async def query(self, pipeline):
        """Compose locate/narrow/read in one bounded, observe-only query."""
        return await self.retrieve("q", options={"pipeline": pipeline})

    async def export(self, path):
        """Export this session's observed handles through the existing capsule."""
        return await self._worker(self._export, Path(path))

    def _export(self, stop, path):
        from ctx.capsule import export
        ws = self._workspace()
        store = Store(ws.workspace_id)
        try:
            return export(store, list(self.handles), path)
        finally:
            store.close()
