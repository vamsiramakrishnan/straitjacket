"""Durable execution context shared by evidence plans, inference and controllers.

One context owns one task allowance. Child namespaces share it; a child never
creates a second budget. Results are immutable artifacts; the existing task
ledger records reservations, outcomes and named checkpoints. This is local
cooperative execution, not a provider billing guarantee or an OS sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
import time

from ctx import taskledger as ledger
from ctx.accounting import charged_seconds
from ctx.store import canonical_json, _atomic_write


class ExecutionPaused(RuntimeError):
    """A closed reason suitable for reports; contains no provider error prose."""


@dataclass(frozen=True)
class OperationResult:
    value: dict
    usage: dict | None = None
    succeeded: bool = True


@dataclass(frozen=True)
class ExecutionLimits:
    max_calls: int = 32
    max_steps: int = 128
    wall_seconds: float = 300.0
    call_seconds: float = 60.0
    max_tokens: int = 131072
    max_output_tokens: int = 2048
    max_cost_usd: float = 1.0
    reserve_cost_usd: float = .05
    response_bytes: int = 64000
    capture_bytes: int = 262144

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0 or value > 1e9
                    or isinstance(f.default, int) and not isinstance(value, int)):
                raise ValueError("invalid execution limit: " + f.name)
        if self.reserve_cost_usd > self.max_cost_usd:
            raise ValueError("one reservation exceeds the cost budget")

    @classmethod
    def from_dict(cls, values):
        return cls(**{f.name: values[f.name] for f in fields(cls) if f.name in values})

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


def publish(store, value, *, evidence=()):
    """Root a JSON artifact and its transitive references under store retention."""
    data = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False).encode()
    ref = "blob:" + store.put_blob(data)
    store.link_artifacts(ref, [value, *evidence])
    store.put_manifest({"schema": "ctx.execution-artifact/v1", "artifact": ref,
                        "document": value, "evidence": list(evidence)}, kind="execution")
    return ref


def read(store, ref):
    full = store.resolve_id(ref.removeprefix("blob:"), kinds=("blob",))
    if not ref.startswith("blob:") or store.blob_path(full).stat().st_size > 16 * 1024 * 1024:
        raise ValueError("invalid execution artifact")
    data = store.get_blob(full)
    if hashlib.sha256(data).hexdigest() != full:
        raise ValueError("execution artifact identity mismatch")
    return json.loads(data)


def totals(operations):
    attempts = list(operations)
    models = [a for a in attempts if a["kind"] == "model"]
    costs = [a.get("usage", {}).get("cost_usd") for a in models]
    return {"steps": len(attempts), "calls": len(models),
        "charged_seconds": sum(charged_seconds(a) for a in attempts),
        "known_cost_usd": sum(c for c in costs if c is not None),
        "reported_cost_usd": sum(costs) if all(c is not None for c in costs) else None,
        "unknown_cost_calls": sum(c is None for c in costs),
        "admitted_cost_usd": sum(max(a["reserved_cost_usd"], c or 0) for a, c in zip(models, costs)),
        "admitted_tokens": sum(max(a["reserved_tokens"], sum(a.get("usage", {}).get(k) or 0
            for k in ("input_tokens", "output_tokens"))) for a in models)}


class TaskRuntime:
    """Public SDK context. Hold ``active()`` around dispatch and checkpoint writes.

    A nonblocking process lock permits one coordinator per task. Parallel
    execution within a task is deliberately not advertised by this version.
    Completed operations replay by identity. Failed/uncertain execution needs
    explicit retry; uncertain mutations require caller-supplied reconciliation.
    """

    def __init__(self, ws, store, task_id, *, namespace="root", retry_failed=False):
        self.ws, self.store, self.task_id = ws, store, task_id
        ledger._check_task_id(task_id)
        self.namespace, self.retry_failed = namespace, retry_failed
        self._active = False
        self._started = None
        self._prior = 0.0
        self.last_pause = None
        spec = self.checkpoint_value("execution")
        if spec is None:
            raise ValueError("task has no execution context")
        self.limits = ExecutionLimits(**spec["limits"])
        self.binding = spec["binding"]
        if self.binding.get("workspace") != str(ws.root.resolve()):
            raise ValueError("execution context belongs to another workspace")

    @classmethod
    def create(cls, ws, store, *, goal, limits=None, binding=None, task_id=None):
        task_id = task_id or ledger.new_task_id()
        if ledger.ledger_path(ws.root, task_id).exists():
            raise ValueError("task already exists")
        limits = ExecutionLimits() if limits is None else limits
        if not isinstance(limits, ExecutionLimits):
            raise TypeError("limits must be ExecutionLimits")
        goal_ref = publish(store, {"goal": goal})
        ledger.append(ws.root, ledger.task_row(task_id, goal_ref=goal_ref, nodes=[],
            budget_usd=limits.max_cost_usd, task_kind="execution", source="sdk"), durable=True)
        ref = publish(store, {"limits": limits.as_dict(),
                             "binding": {**(binding or {}), "workspace": str(ws.root.resolve())}})
        ledger.append(ws.root, {"schema": ledger.STATE_SCHEMA, "task_id": task_id,
                                "name": "execution", "ref": ref}, durable=True)
        return cls(ws, store, task_id)

    def state(self):
        return ledger.task_state(ledger.load(self.ws.root, self.task_id))

    def checkpoint_value(self, name, default=None):
        ref = self.state().checkpoints.get(name)
        return read(self.store, ref) if ref else default

    def checkpoint(self, name, value, *, evidence=()):
        self._require_active()
        if name == "execution":
            raise ValueError("execution limits and bindings are immutable; create a new task")
        ref = publish(self.store, value, evidence=evidence)
        ledger.append(self.ws.root, {"schema": ledger.STATE_SCHEMA, "task_id": self.task_id,
                                    "name": name, "ref": ref}, durable=True)
        self.retain()
        return ref

    def retain(self):
        """Renew one root for this execution's complete artifact dependency graph."""
        state = self.state()
        return publish(self.store, {"schema": "ctx.execution-root/v1", "task_id": self.task_id,
            "task": state.task, "checkpoints": state.checkpoints,
            "operations": list(state.operations.values())})

    def _require_active(self):
        if not self._active:
            raise RuntimeError("hold runtime.active() before execution or checkpointing")

    @contextmanager
    def active(self):
        if self._active:
            raise RuntimeError("execution context is already active")
        if os.name != "posix":
            raise RuntimeError("durable execution currently requires POSIX locks")
        import fcntl
        path = ledger.ledger_path(self.ws.root, self.task_id).with_suffix(".execution.lock")
        with path.open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExecutionPaused("task_already_running") from exc
            try:
                self._active = True
                self.last_pause = None
                for row in self.state().operations.values():
                    if row["status"] == "running":
                        ledger.append(self.ws.root, dict(row, status="uncertain"), durable=True)
                self._prior = self.totals()["charged_seconds"]
                existing = set(self.state().operations)
                overhead = self.checkpoint_value("execution.time", {}).get("overhead_seconds", 0)
                self._started = time.monotonic()
                yield self
            finally:
                try:
                    if self._started is not None:
                        observed = sum(a.get("elapsed_seconds", 0) for key, a in self.state().operations.items()
                                       if key not in existing)
                        self.checkpoint("execution.time", {"overhead_seconds": overhead +
                            max(0, time.monotonic() - self._started - observed)})
                finally:
                    self._active = False
                    self._started = None
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def child(self, name):
        """A namespace view, with the same journal, limits and cancellation."""
        return ExecutionScope(self, self.namespace + "/" + name)

    def totals(self):
        result = totals(self.state().operations.values())
        result["charged_seconds"] += self.checkpoint_value("execution.time", {}).get("overhead_seconds", 0)
        return result

    def remaining(self):
        # Include bookkeeping while live; across restarts retain all observed
        # operation time and reservations for unknown outcomes.
        charged = self.totals()["charged_seconds"]
        if self._started is not None:
            charged = max(charged, self._prior + time.monotonic() - self._started)
        return max(0.0, self.limits.wall_seconds - charged)

    @property
    def cancellation_path(self):
        return ledger.ledger_path(self.ws.root, self.task_id).with_suffix(".cancel")

    def cancel(self):
        _atomic_write(self.cancellation_path, b"cancelled\n")

    def cancelled(self):
        return self.cancellation_path.exists()

    def pause(self, reason):
        self.last_pause = reason
        raise ExecutionPaused(reason)

    def perform(self, key, op, request, fn, *, kind="observe", timeout=None,
                model_bytes=0, reconcile=None):
        self._require_active()
        if kind not in {"observe", "execute", "mutate", "model"}:
            raise ValueError("unknown execution kind")
        timeout = self.limits.call_seconds if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("operation timeout must be a positive finite number")
        signature = hashlib.sha256(canonical_json({"op": op, "request": request})).hexdigest()
        operation_key = self.namespace + "/" + key
        prior = [a for a in self.state().operations.values() if a["key"] == operation_key]
        for a in prior:
            if a["signature"] != signature:
                raise ValueError("operation key reused with different inputs")
            if a["status"] == "done":
                return read(self.store, a["result"])
        if prior:
            last = prior[-1]
            if kind == "mutate" and last["status"] == "uncertain":
                if reconcile is None:
                    self.pause("uncertain_mutation")
                recovered = reconcile(last)
                if recovered is not None:
                    ref = publish(self.store, recovered, evidence=[last["request"]])
                    # Retain the unknown time charge even after byte reconciliation.
                    elapsed = charged_seconds(last)
                    ledger.append(self.ws.root, dict(last, status="done", result=ref,
                        elapsed_seconds=elapsed, recovered=True), durable=True)
                    return recovered
            if not self.retry_failed:
                self.pause("retry_requires_explicit_request")
        if self.cancelled():
            self.pause("cancelled")
        remaining = self.remaining()
        usage = self.totals()
        cost = self.limits.reserve_cost_usd if kind == "model" else 0
        tokens = model_bytes + self.limits.max_output_tokens if kind == "model" else 0
        if remaining <= 0:
            self.pause("wall_budget")
        if usage["steps"] >= self.limits.max_steps:
            self.pause("step_budget")
        if kind == "model":
            if usage["calls"] >= self.limits.max_calls:
                self.pause("call_budget")
            if usage["admitted_cost_usd"] + cost > self.limits.max_cost_usd + 1e-12:
                self.pause("cost_admission_budget")
            if usage["admitted_tokens"] + tokens > self.limits.max_tokens:
                self.pause("token_admission_budget")
        timeout = min(timeout, remaining)
        req = publish(self.store, request)
        row = {"schema": ledger.OPERATION_SCHEMA, "task_id": self.task_id,
            "operation_id": hashlib.sha256((operation_key + str(len(prior))).encode()).hexdigest(),
            "key": operation_key, "op": op, "kind": kind, "signature": signature,
            "status": "running", "request": req, "reserved_seconds": timeout,
            "reserved_cost_usd": cost, "reserved_tokens": tokens}
        ledger.append(self.ws.root, row, durable=True)  # cannot dispatch if persistence fails
        self.retain()
        started = time.monotonic()
        try:
            returned = fn(timeout)
            outcome = returned if isinstance(returned, OperationResult) else OperationResult(returned)
            result = outcome.value
            if not isinstance(result, dict):
                raise TypeError("operations return an artifact object")
            row["usage"] = valid_usage(outcome.usage or {}) if kind == "model" else {}
            ref = publish(self.store, result, evidence=[req])
            row.update(status="done" if outcome.succeeded else "failed", result=ref)
            return result
        except Exception as exc:
            row.update(status="failed", error=type(exc).__name__)
            raise
        except BaseException:
            row.update(status="uncertain")
            raise
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 6)
            ledger.append(self.ws.root, row, durable=True)
            self.retain()

    def model_worker(self, worker, *, namespace, validate=None):
        """Wrap any JSON worker with this root allowance, including semantic maps."""
        from ctx.semantic.worker import WorkerResult
        def invoke(data, *, timeout, response_bytes):
            key = namespace + "/" + hashlib.sha256(data).hexdigest()
            def call(allowance):
                out = worker(data, timeout=allowance, response_bytes=response_bytes)
                if len(out.stdout) > response_bytes or len(out.stderr) > min(16000, response_bytes):
                    raise ValueError("worker exceeded response limit")
                result = {"stdout": "blob:" + self.store.put_blob(out.stdout),
                          "stderr": "blob:" + self.store.put_blob(out.stderr),
                          "error": out.error, "returncode": out.returncode,
                          "usage": extract_usage(out.stdout)}
                try:
                    self.store.link_artifacts(result["stdout"], json.loads(out.stdout))
                except (ValueError, UnicodeError):
                    pass
                if validate is not None and not out.error and out.returncode in (None, 0):
                    try:
                        validate(out.stdout)
                    except (ValueError, TypeError, KeyError):
                        result["error"] = "invalid_worker_response"
                return OperationResult(result, usage=result["usage"],
                                       succeeded=not result["error"] and out.returncode in (None, 0))
            result = self.perform(key, "model.invoke", {"request": data.decode("utf-8")}, call,
                                  kind="model", timeout=timeout, model_bytes=len(data))
            return WorkerResult(self.store.get_blob(result["stdout"][5:]),
                self.store.get_blob(result["stderr"][5:]), result["error"], result["returncode"])
        return invoke


def valid_usage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("input_tokens", "output_tokens", "cost_usd"):
        v = value.get(key)
        if v is None:
            continue
        if (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
                or key.endswith("tokens") and not isinstance(v, int)):
            continue
        result[key] = v
    return result


def extract_usage(data):
    try:
        return valid_usage(json.loads(data).get("usage", {}))
    except (ValueError, AttributeError, UnicodeError):
        return {}


class ExecutionScope:
    """Namespace-only view; forwards accounting and state to its root context."""
    def __init__(self, root, namespace):
        self.root, self.namespace = root, namespace

    def perform(self, key, *args, **kwargs):
        return self.root.perform(self.namespace + "/" + key, *args, **kwargs)

    def child(self, name):
        return ExecutionScope(self.root, self.namespace + "/" + name)
