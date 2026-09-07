"""Resumable depth-one semantic map with one root admission budget.

Only this coordinator writes to Store. A nonblocking per-plan file lock keeps
two processes from spending the same reservation. A claim is durable before
launch; unknown attempts consume their reservation until explicitly retried.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import time

from ctx.semantic.contract import SemanticError, identity, object_fields, number, response
from ctx.semantic.evidence import load_plan, publish, read_document, worker_request
from ctx.semantic.worker import CommandWorker
from ctx.store import _atomic_write, canonical_json
from ctx.accounting import charged_seconds


@contextmanager
def _lock(path):
    if os.name != "posix":
        raise SemanticError("semantic execution currently requires POSIX process and file locks")
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SemanticError("this semantic map is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _paths(store, handle):
    # Resolve aliases before choosing the lock: short/full handles share it.
    full = store.resolve_id(handle.removeprefix("blob:"), kinds=("blob",))
    return "blob:" + full, store.root / "semantic" / (full + ".json")


def _initial(plan_ref):
    return {"schema": "ctx.semantic.state/v1", "plan": plan_ref, "attempts": []}


def _load_state(store, path, plan_ref):
    if not path.exists():
        return _initial(plan_ref)
    state = read_document(store, json.loads(path.read_bytes())["state"])
    if state.get("schema") != "ctx.semantic.state/v1" or state.get("plan") != plan_ref:
        raise SemanticError("semantic checkpoint does not match the plan")
    return state


def _roots(plan, state):
    return [state["plan"], *[s[key] for s in plan["sources"] for key in ("source", "ref")],
            *[a[key] for a in state["attempts"] for key in ("request", "stdout", "stderr", "result") if key in a]]


def _save(store, path, plan, state):
    state_ref = publish(store, state, evidence=_roots(plan, state))
    _atomic_write(path, canonical_json({"state": state_ref}))


def _totals(state):
    attempts = state["attempts"]
    costs = [a.get("usage", {}).get("cost_usd") for a in attempts]
    elapsed = [a.get("elapsed_seconds") for a in attempts]
    return {"calls": len(attempts),
            "reported_cost_usd": sum(c for c in costs if c is not None) if all(c is not None for c in costs) else None,
            "known_cost_usd": sum(c for c in costs if c is not None),
            "unknown_cost_calls": sum(c is None for c in costs),
            "admitted_cost_usd": sum(max(a["reserved_cost_usd"], c or 0) for a, c in zip(attempts, costs)),
            "admitted_tokens": sum(max(a["reserved_tokens"],
                                        sum(a.get("usage", {}).get(k) or 0 for k in ("input_tokens", "output_tokens")))
                                    for a in attempts),
            "elapsed_seconds": sum(t or 0 for t in elapsed) if all(t is not None for t in elapsed) else None,
            "charged_seconds": sum(charged_seconds(a) for a in attempts),
            **{key: sum(a.get("usage", {}).get(key) or 0 for a in attempts)
               if all(a.get("usage", {}).get(key) is not None for a in attempts) else None
               for key in ("input_tokens", "output_tokens")}}


def _report(store, plan, state, reason=None):
    completed = {a["partition"]: a for a in state["attempts"] if a["status"] == "done"}
    findings, unresolved = {}, set()
    for a in completed.values():
        result = read_document(store, a["result"])
        # Exact duplicates only; equivalence and contradiction are model judgments.
        for finding in result["findings"]:
            findings[identity(finding)] = finding
        unresolved.update(result["unresolved"])
    partitions = plan["partitions"]
    coverage = {"completed_partitions": len(completed), "selected_partitions": len(partitions),
                "processing_basis": "validated_partition_responses",
                "processed_bytes": sum(p["bytes"] for i, p in enumerate(partitions) if i in completed),
                "selected_bytes": sum(p["bytes"] for p in partitions),
                "processing_complete": len(completed) == len(partitions),
                "selection_complete": None, "selection_note": plan["spec"]["selection_note"]}
    report = {"schema": "ctx.semantic.report/v1", "plan": state["plan"],
              "status": "complete" if coverage["processing_complete"] else "partial",
              "stop_reason": reason, "epistemic_status": "model_inference",
              "citation_validation": "membership_and_lines_only", "coverage": coverage,
              "totals": _totals(state), "findings": [findings[k] for k in sorted(findings)],
              "unresolved": sorted(unresolved), "attempts": state["attempts"],
              "sources": plan["sources"], "worker": plan["spec"]["worker"],
              "limits": plan["spec"]["limits"], "max_depth": 1, "max_concurrency": 1,
              "calls_scope": "worker_attempts",
              "state": publish(store, state, evidence=_roots(plan, state))}
    if coverage["processing_complete"]:
        report["stop_reason"] = None
    report["usage_source"] = "driver_reported; identity and billing are not independently authenticated"
    report_ref = publish(store, report, evidence=_roots(plan, state))
    return report_ref, report


def inspect(ws, store, plan_ref):
    """Read the latest committed checkpoint, including while a worker runs."""
    plan = load_plan(ws, store, plan_ref)
    plan_ref, path = _paths(store, plan_ref)
    return _report(store, plan, _load_state(store, path, plan_ref))


def _usage(data):
    """Keep valid accounting even when the findings themselves are rejected."""
    from ctx.semantic.contract import parse_json
    try:
        value = parse_json(data)
        usage = object_fields(value.get("usage", {}), {"input_tokens", "output_tokens", "cost_usd"})
        for key, v in usage.items():
            if v is not None:
                number(v, integer=key.endswith("tokens"))
        return usage
    except (SemanticError, AttributeError):
        return {}


def run(ws, store, plan_ref, *, worker=None, retry_failed=False, stop_on_failure=False,
        runtime=None):
    """Execute unfinished partitions; durable results are reused only in this map.

    A new ``sample`` or any changed input/model/settings creates a new plan.
    Retrying a failed/uncertain attempt requires an explicit flag, another
    reservation, and remaining root budget. There are no automatic retries.
    """
    plan = load_plan(ws, store, plan_ref)
    plan_ref, path = _paths(store, plan_ref)
    limits = plan["spec"]["limits"]
    if worker is None:
        command = plan["spec"]["worker"].get("command")
        if not command:
            raise SemanticError("plan has no command driver; supply an SDK worker or prepare with worker.command")
        worker = CommandWorker(command)
    if runtime is not None:
        stop_on_failure = True
    with _lock(path.with_suffix(".lock")):
        state = _load_state(store, path, plan_ref)
        # A lost process cannot prove whether an external provider billed it.
        # Preserve its reservation and never quietly repeat the request.
        for attempt in state["attempts"]:
            if attempt["status"] == "running":
                attempt["status"] = "uncertain"
        _save(store, path, plan, state)
        initial_time = time.monotonic()
        prior_seconds = _totals(state)["charged_seconds"]
        reason = None
        for i, part in enumerate(plan["partitions"]):
            prior = [a for a in state["attempts"] if a["partition"] == i]
            if any(a["status"] == "done" for a in prior) or (prior and not retry_failed):
                continue
            data = worker_request(store, plan, part)
            # A deliberately conservative text estimate, not a tokenizer or a
            # bound on hidden driver/tool traffic. Actual usage reconciles it.
            reserved_tokens = len(data) + limits["max_output_tokens"]
            totals = _totals(state)
            remaining = limits["wall_seconds"] - prior_seconds - (time.monotonic() - initial_time)
            if totals["calls"] >= limits["max_calls"]:
                reason = "call_budget"
            elif totals["admitted_cost_usd"] + limits["reserve_cost_usd"] > limits["max_cost_usd"] + 1e-12:
                reason = "cost_admission_budget"
            elif remaining <= 0:
                reason = "wall_budget"
            elif totals["admitted_tokens"] + reserved_tokens > limits["max_tokens"]:
                reason = "token_admission_budget"
            if reason:
                break
            timeout = min(limits["call_seconds"], remaining)
            attempt = {"partition": i, "status": "running", "request": "blob:" + store.put_blob(data),
                       "request_key": identity({"request": data.decode("utf-8"), "worker": plan["spec"]["worker"]}),
                       "reserved_cost_usd": limits["reserve_cost_usd"], "reserved_seconds": timeout,
                       "reserved_tokens": reserved_tokens}
            state["attempts"].append(attempt)
            _save(store, path, plan, state)  # durable before any billable action
            started = time.monotonic()
            try:
                call_worker = (runtime.model_worker(worker, namespace=f"semantic/{plan_ref}/{i}/{len(prior)}",
                                                   validate=lambda raw: response(raw, part))
                               if runtime is not None else worker)
                outcome = call_worker(data, timeout=timeout, response_bytes=limits["response_bytes"])
                if len(outcome.stdout) > limits["response_bytes"] or len(outcome.stderr) > min(16000, limits["response_bytes"]):
                    raise SemanticError("SDK worker exceeded response_bytes")
                attempt.update(stdout="blob:" + store.put_blob(outcome.stdout),
                               stderr="blob:" + store.put_blob(outcome.stderr),
                               usage=_usage(outcome.stdout), returncode=outcome.returncode)
                if outcome.error or outcome.returncode not in (None, 0):
                    attempt.update(status="failed", error=(outcome.error or "worker_failed")[:128])
                else:
                    result = response(outcome.stdout, part)
                    attempt.update(status="done", result=publish(store, result, evidence=[part["ref"]]))
            except Exception as exc:
                # No exception prose goes into a model-visible report (provider
                # errors may contain credentials or arbitrary unbounded output).
                attempt.update(status="failed", error=type(exc).__name__)
            except BaseException:
                attempt.update(status="uncertain")
                raise
            finally:
                attempt["elapsed_seconds"] = round(time.monotonic() - started, 6)
                _save(store, path, plan, state)
            if stop_on_failure and attempt["status"] != "done":
                reason = "failed_or_uncertain_attempts"
                break
        if reason is None and any(a["status"] != "done" for a in state["attempts"]):
            reason = "failed_or_uncertain_attempts"
        result = _report(store, plan, state, reason)
        if runtime is not None and runtime.last_pause:
            from ctx.task_runtime import ExecutionPaused
            raise ExecutionPaused(runtime.last_pause)
        return result
