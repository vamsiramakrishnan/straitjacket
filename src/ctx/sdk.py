"""Async tool boundary for any embedding harness; no provider SDK dependency.

Every worker owns its Store connection. Process cancellation is cooperative
at capture, shielded until the owned process group is stopped and partial
evidence has been published. No process-global cwd or environment mutation.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from ctx.execution import run_capture
from ctx.store import Store, canonical_json
from ctx.toolresult import CommandResult, render_command_result
from ctx.tooltelemetry import record_tool_call
from ctx.workspace import resolve_workspace

READ_OPS = frozenset({"get", "search", "stats", "map", "def", "refs", "diag",
                     "callers", "callees", "impact", "diff", "q", "repo", "investigate"})


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

    def __init__(self, workspace, *, timeout=600, max_tokens=None, env=None, producer="sdk",
                 durable=False, task_id=None, limits=None, context_epoch=None, retry_failed=False):
        self.root = resolve_workspace(str(workspace)).root
        self.timeout = _timeout(timeout)
        if max_tokens is not None and (type(max_tokens) is not int or not 64 <= max_tokens <= 4000):
            raise ValueError("max_tokens must be an integer in 64..4000")
        self.max_tokens = max_tokens
        self.env = dict(env or {})
        self.producer = producer
        self._refs: set[str] = set()
        self._lock = threading.Lock()
        self._metrics = {"custom_tool_calls": 0, "delivered_bytes": 0, "retrieval_calls": 0}
        self._execution_lock = threading.RLock()
        self._delivery = {}
        self.set_context_epoch(uuid.uuid4().hex if context_epoch is None else context_epoch)
        # Hosts that cannot signal context loss must receive full reads.
        self.reuse = context_epoch is not None
        self.task_id = None
        self.retry_failed = retry_failed
        if durable or task_id is not None or limits is not None:
            from ctx.task_runtime import TaskRuntime
            from ctx import taskledger
            ws = self._workspace()
            store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            try:
                task_id = taskledger.new_task_id() if task_id is None else task_id
                taskledger._check_task_id(task_id)
                if taskledger.ledger_path(ws.root, task_id).exists():
                    runtime = TaskRuntime(ws, store, task_id)
                    if limits is not None and runtime.limits != limits:
                        raise ValueError("execution limits are immutable on resume")
                else:
                    runtime = TaskRuntime.create(ws, store, task_id=task_id,
                        goal="ToolSession evidence and repair", limits=limits)
                self.task_id = runtime.task_id
                state = runtime.checkpoint_value("tools.session", {})
                self._admit(" ".join(state.get("handles", [])))
                if state.get("epoch") == self.context_epoch:
                    self._delivery = state.get("files", {})
            finally:
                store.close()

    def set_context_epoch(self, epoch):
        """Call after context loss/compaction; reuse never crosses this boundary."""
        if not isinstance(epoch, str) or not epoch or len(epoch) > 200:
            raise ValueError("context_epoch needs 1..200 characters")
        with self._execution_lock:
            if epoch != getattr(self, "context_epoch", None):
                self._delivery = {}
            self.context_epoch = epoch
            self.reuse = True

    @contextmanager
    def _scope(self, ws, store, stop, *, serialize=False):
        from ctx.task_runtime import TaskRuntime, ExecutionPaused
        locked = self.task_id is not None or serialize
        if locked:
            while not self._execution_lock.acquire(timeout=.05):
                if stop.is_set():
                    raise ExecutionPaused("cancelled")
        try:
            if self.task_id is None:
                try:
                    yield None
                finally:
                    if stop.is_set():
                        self._delivery = {}
                return
            runtime = TaskRuntime(ws, store, self.task_id, retry_failed=self.retry_failed)
            with runtime.active():
                state = runtime.checkpoint_value("tools.session", {})
                self._admit(" ".join(state.get("handles", [])))
                self._delivery = state.get("files", {}) if state.get("epoch") == self.context_epoch else {}
                try:
                    if stop.is_set():
                        raise ExecutionPaused("cancelled")
                    yield runtime
                finally:
                    if stop.is_set():
                        self._delivery = {}
                    runtime.checkpoint("tools.session", {"epoch": self.context_epoch,
                        "files": self._delivery, "handles": list(self.handles)}, evidence=self.handles)
        finally:
            if locked:
                self._execution_lock.release()

    def _perform(self, runtime, key, op, request, function, *, kind="observe", timeout=None):
        if key is not None and (not isinstance(key, str) or not key or len(key) > 200):
            raise ValueError("operation_key needs 1..200 characters")
        if runtime is None:
            return function(self.timeout if timeout is None else timeout)
        if kind == "mutate" and any(r["kind"] == "mutate" and r["status"] == "uncertain"
                                    for r in runtime.state().operations.values()):
            runtime.pause("uncertain_mutation")
        key = key or f"tool-{len(runtime.state().operations):06d}"
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError("operation_key needs 1..200 characters")
        return runtime.perform(key, op, request, function, kind=kind,
                               timeout=min(self.timeout if timeout is None else timeout, runtime.limits.call_seconds))

    @property
    def handles(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._refs))

    @property
    def metrics(self):
        with self._lock:
            return dict(self._metrics)

    def _record(self, store, **row):
        with self._lock:
            self._metrics["custom_tool_calls"] += 1
            self._metrics["delivered_bytes"] += row["delivered_bytes"]
            self._metrics["retrieval_calls"] += row["op"] not in {"run", "repair"}
        record_tool_call(store, **row)

    def _admit(self, text):
        with self._lock:
            self._refs.update(m.group(0) for m in re.finditer(r"\b(?:run|blob|snapshot):[0-9a-f]{6,64}\b", text))

    def _workspace(self):
        # Refresh gitHead and committed policy after edits/commits. Keep budgets
        # on a per-call copy; parallel tools cannot loosen one another's policy.
        ws = resolve_workspace(str(self.root))
        # The embedding API promises one root even when the interactive CLI's
        # committed policy permits outside reads. Tighten the existing path
        # mechanism rather than translating an outside cwd back into the root.
        ws = replace(ws, config=replace(ws.config,
            workspace=replace(ws.config.workspace, allow_outside_root=False)))
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
                  focus=None, delivery="auto", operation_key=None) -> CommandResult:
        """Execute argv, or an explicit Bash command string, with typed status."""
        return await self._command(command, working_directory, timeout_seconds, focus, delivery, operation_key, False)

    async def run_model(self, command, *, working_directory="", timeout_seconds=None,
                        focus=None, delivery="auto", operation_key=None) -> str:
        """Execute with compact model delivery and measure the actual view bytes."""
        return await self._command(command, working_directory, timeout_seconds, focus, delivery, operation_key, True)

    async def _command(self, command, working_directory, timeout_seconds, focus, delivery, operation_key, model_view):
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
        return await self._worker(self._run, argv, working_directory, timeout, focus, delivery, operation_key, model_view)

    def _run(self, stop, argv, cwd, timeout, focus, delivery, operation_key, model_view):
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
            with self._scope(ws, store, stop) as runtime:
                env = {**os.environ, **self.env}
                def capture_command(allowance):
                    from ctx.task_runtime import OperationResult
                    capture = run_capture(ws, argv, cwd=ws.relativize(workdir) or ".", timeout=allowance,
                        store=store, cancelled=lambda: stop.is_set() or bool(runtime and runtime.cancelled()), env=env,
                        output_bytes=runtime.limits.capture_bytes if runtime else None)
                    result = render_command_result(ws, store, capture, focus=focus, delivery=delivery)
                    self._admit(result.ref)
                    value = {"result": result.as_dict(), "raw_bytes": sum(s["bytes"] for s in capture.manifest["streams"].values())}
                    return OperationResult(value, succeeded=not (result.timed_out or result.cancelled or result.output_limited)) if runtime else value
                value = self._perform(runtime, operation_key, "tool.run", {"argv": argv,
                    "cwd": ws.relativize(workdir) or ".", "focus": focus, "delivery": delivery,
                    "environmentHash": hashlib.sha256(canonical_json(env)).hexdigest(),
                    "budget": ws.config.budgets.result_tokens}, capture_command, kind="mutate", timeout=timeout)
                result = CommandResult.from_dict(value["result"])
                self._admit(result.ref)
            text = result.to_model(max_bytes=ws.config.budgets.result_tokens * 4) if model_view else result.to_json()
            delivered = 0 if stop.is_set() else len(text.encode())
            raw = value["raw_bytes"]
            record_telemetry(store, "run", raw, delivered)
            self._record(store, op="run", delivered_bytes=delivered,
                elapsed_seconds=time.monotonic()-started, captured_bytes=raw, result=result,
                producer=self.producer)
            return text if model_view else result
        except Exception as exc:
            self._record(store, op="run", delivered_bytes=0, elapsed_seconds=time.monotonic()-started,
                error=type(exc).__name__, producer=self.producer)
            raise
        finally:
            store.close()

    async def retrieve(self, op, *, ref=None, patterns=None, selector=None, options=None, operation_key=None) -> str:
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
            "patterns": list(patterns or []), "selector": dict(selector or {}), "options": dict(options or {})}, operation_key)

    def _retrieve(self, stop, args, operation_key):
        from ctx.mcp import dispatch_bounded
        started = time.monotonic()
        ws = self._workspace()
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        try:
            if stop.is_set():
                return ""
            # No workspace override, task/send or execute operations admitted.
            with self._scope(ws, store, stop, serialize=True) as runtime:
                # Plans charge their leaves to this same root rather than a
                # second nested budget or a single unaccounted batch.
                if args["op"] == "investigate":
                    key = operation_key or f"plan-{len(runtime.state().operations) if runtime else 0}"
                    text = dispatch_bounded(ws, store, args, runtime=runtime.child(key) if runtime else None)
                else:
                    value = self._perform(runtime, operation_key, "tool." + args["op"], args,
                        lambda allowance: {"text": dispatch_bounded(ws, store, args)})
                    text = value["text"]
                self._admit(text)
            self._record(store, op=args["op"], delivered_bytes=0 if stop.is_set() else len(text.encode("utf-8", "surrogateescape")),
                elapsed_seconds=time.monotonic()-started, producer=self.producer)
            return text
        except Exception as exc:
            self._record(store, op=args["op"], delivered_bytes=0, elapsed_seconds=time.monotonic()-started,
                error=type(exc).__name__, producer=self.producer)
            raise
        finally:
            store.close()

    async def get(self, ref, *, start_line, end_line, fresh=False, operation_key=None):
        if type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line:
            raise ValueError("get needs an inclusive positive line range")
        from ctx.refs import parse_ref
        parsed = parse_ref(ref)
        if parsed.kind == "repo" and not parsed.workspace_alias:
            return await self.navigate(parsed.path, start_line=start_line, end_line=end_line,
                                       fresh=fresh, operation_key=operation_key)
        return await self.retrieve("get", ref=ref, selector={"lines": f"{start_line}:{end_line}"}, operation_key=operation_key)

    async def navigate(self, path, *, symbol=None, start_line=None, end_line=None,
                       mode="auto", fresh=False, operation_key=None):
        """Small files/ranges read directly; large files outline before bodies.

        Reuse applies only to complete lines delivered in this context epoch.
        Set fresh=True for a full view, including previously delivered lines.
        """
        if not isinstance(path, str) or not path or path.startswith(("ws:", "snapshot:", "run:", "blob:")):
            raise ValueError("navigate needs a repository path")
        if path.startswith("repo:"):
            path = path[5:]
        if mode not in {"auto", "outline"} or type(fresh) is not bool:
            raise ValueError("invalid navigation mode or fresh flag")
        if (start_line is None) != (end_line is None) or start_line is not None and (
            type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line):
            raise ValueError("navigate needs an inclusive positive line range")
        if symbol is not None and (not isinstance(symbol, str) or not symbol or start_line is not None):
            raise ValueError("choose a symbol or line range")
        return await self._worker(self._navigate, path, symbol, start_line, end_line, mode, fresh, operation_key)

    def _navigate(self, stop, path, symbol, start, end, mode, fresh, operation_key):
        from ctx.toolviews import navigate, MAX_SOURCE_BYTES
        from ctx.execution import snapshot_file
        ws = self._workspace()
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        started = time.monotonic()
        try:
            with self._scope(ws, store, stop, serialize=True) as runtime:
                snap = snapshot_file(store, ws, path, max_bytes=MAX_SOURCE_BYTES)
                def observe(allowance):
                    text, refs = navigate(ws, store, path, self._delivery if self.reuse else {}, symbol=symbol,
                        start=start, end=end, mode=mode, fresh=fresh, snapshot=snap)
                    self._admit(" ".join(refs))
                    return {"text": text, "refs": refs}
                value = self._perform(runtime, operation_key, "tool.navigate", {
                    "path": path, "source": snap["blob"], "symbol": symbol,
                    "start": start, "end": end, "mode": mode, "fresh": fresh,
                    "epoch": hashlib.sha256(self.context_epoch.encode()).hexdigest(),
                    "reuse": self.reuse,
                    "budget": ws.config.budgets.result_tokens}, observe)
                self._admit(" ".join(value["refs"]))
            text = value["text"]
            self._record(store, op="navigate", delivered_bytes=0 if stop.is_set() else len(text.encode()),
                elapsed_seconds=time.monotonic()-started, producer=self.producer)
            return text
        except Exception as exc:
            self._record(store, op="navigate", delivered_bytes=0, elapsed_seconds=time.monotonic()-started,
                error=type(exc).__name__, producer=self.producer)
            raise
        finally:
            store.close()

    async def search(self, ref, pattern, *, context=3, fixed=False):
        if type(context) is not int or not 0 <= context <= 100:
            raise ValueError("context must be an integer in 0..100")
        return await self.retrieve("search", ref=ref, patterns=[pattern], options={"context": context, "fixed": fixed})

    async def query(self, pipeline, *, operation_key=None):
        """Compose locate/narrow/read in one bounded, observe-only query."""
        return await self.retrieve("q", options={"pipeline": pipeline}, operation_key=operation_key)

    async def investigate(self, plan, *, operation_key=None):
        """Execute an observe-only evidence plan with aggregate bounded delivery."""
        return await self.retrieve("investigate", options={"plan": plan}, operation_key=operation_key)

    async def repair(self, failure_ref, *, request=None, edit_receipt=None, targeted,
                     regression, witnesses=(), operation_key=None):
        """Apply an anchored request (or verify a receipt), then check exact bytes.

        ``targeted`` and ``regression`` are lists of edit_verification.Check.
        The first targeted argv must reproduce the observed failing command.
        """
        return await self._start_repair(failure_ref, request, edit_receipt, targeted, regression, witnesses, operation_key, False)

    async def repair_model(self, failure_ref, *, request=None, edit_receipt=None, targeted,
                           regression, witnesses=(), operation_key=None):
        """Repair with a bounded model summary; full proof stays addressable."""
        return await self._start_repair(failure_ref, request, edit_receipt, targeted, regression, witnesses, operation_key, True)

    async def _start_repair(self, failure_ref, request, edit_receipt, targeted, regression, witnesses, operation_key, model_view):
        if (request is None) == (edit_receipt is None):
            raise ValueError("provide an edit request or an applied edit receipt")
        return await self._worker(self._repair, failure_ref, request, edit_receipt,
                                  list(targeted), list(regression), tuple(witnesses), operation_key, model_view)

    def _repair(self, stop, failure_ref, request, edit_receipt, targeted, regression, witnesses, operation_key, model_view):
        from ctx.toolrepair import repair
        ws = self._workspace()
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        started = time.monotonic()
        try:
            with self._scope(ws, store, stop, serialize=True) as runtime:
                key = operation_key or f"repair-{len(runtime.state().operations) if runtime else 0}"
                result = repair(ws, store, failure_ref, request=request, edit_receipt=edit_receipt,
                    targeted=targeted, regression=regression, witnesses=witnesses, key=key,
                    perform=lambda name, op, inputs, fn, **kwargs: self._perform(runtime, name, op, inputs, fn, **kwargs),
                    cancelled=lambda: stop.is_set() or bool(runtime and runtime.cancelled()),
                    env={**os.environ, **self.env},
                    capture_bytes=runtime.limits.capture_bytes if runtime else 262144)
                self._admit(" ".join([result["repairRef"], result["verificationRef"], result["editReceipt"],
                                      *(c["runRef"] for c in result["checks"])]))
            if model_view:
                from ctx.toolresult import fit_model_view
                head = f"[ctx repair outcome={result['outcome']}]\nproof: {result['repairRef']}\n"
                body = "\n".join(f"{c['role']} {c['kind']}: {'passed' if c['passed'] else 'failed'} {c['runRef']}" for c in result["checks"])
                body += f"\nchecks completed: {len(result['checks'])}/{len(targeted)+len(regression)}; inputs: {len(result['inputs'])} files\nfull: ctx get {result['repairRef']}"
                returned = fit_model_view(head, body, result["repairRef"], ws.config.budgets.result_tokens * 4)
            else:
                returned = result
            self._record(store, op="repair", delivered_bytes=0 if stop.is_set() else (len(returned.encode()) if model_view else len(canonical_json(returned))),
                elapsed_seconds=time.monotonic()-started, producer=self.producer)
            return returned
        except Exception as exc:
            self._record(store, op="repair", delivered_bytes=0, elapsed_seconds=time.monotonic()-started,
                error=type(exc).__name__, producer=self.producer)
            raise
        finally:
            store.close()

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
