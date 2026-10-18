"""One repair receipt joins an observed failure, CAS edit, and bounded checks.

All writes and verification reuse the transaction and verification engines.
The caller supplies commands; captured text never selects executable actions.
"""
from __future__ import annotations

import hashlib

from ctx.edit_transactions import create_edit_plan, apply_edit_plan
from ctx.edit_verification import (VerificationError, file_digests, read_evidence,
                                   validate_checks, verify_edit, validate_verification)
from ctx.execution import run_capture, CaptureResult
from ctx.task_runtime import publish
from ctx.store import canonical_json


def repair(ws, store, failure_ref, *, request, edit_receipt, targeted, regression,
           witnesses, key, perform, cancelled, capture_bytes, env):
    validate_checks([*targeted, *regression])
    if not targeted or not regression or targeted[0].kind != "behavior":
        raise VerificationError("repair requires targeted behavioral and regression checks")
    if not isinstance(failure_ref, str) or not failure_ref.startswith("run:"):
        raise VerificationError("repair requires an observed run: failure")
    failure_id = store.resolve_id(failure_ref[4:], kinds=("run",))
    failure = store.get_manifest(failure_id)
    status = failure.get("result", {})
    if (failure.get("workspaceId") != ws.workspace_id or status.get("exitCode") == 0
            or any(status.get(k) for k in ("timedOut", "cancelled", "outputLimited"))
            or status.get("exitCode") is None and not status.get("signal")):
        raise VerificationError("failure must be a completed failing command in this workspace")
    if list(targeted[0].argv) != failure.get("argv"):
        raise VerificationError("first targeted check must rerun the observed failing argv")
    cwd = failure.get("cwd", ".")
    ws.confine(cwd, must_exist=True)
    # Validate all requested input paths before committing a source change.
    file_digests(ws, witnesses)
    if cancelled():
        raise VerificationError("repair cancelled before edit")
    if request is not None:
        plan = perform(key + "/plan", "edit.plan", request,
            lambda timeout: create_edit_plan(ws, store, request))
        plan_ref = publish(store, plan)
        def apply(timeout):
            if cancelled():
                raise VerificationError("repair cancelled before edit")
            receipt = apply_edit_plan(ws, plan, attempt_key=key)
            if receipt.get("evidenceError") or not receipt.get("receiptRef"):
                raise VerificationError("edit applied but receipt persistence failed")
            return receipt
        receipt = perform(key + "/apply", "edit.apply", {"plan": plan_ref}, apply, kind="mutate")
        edit_receipt = receipt["receiptRef"]
    else:
        read_evidence(store, edit_receipt, "ctx.edit-receipt/v1")
    index = 0
    def runner(workspace, argv, *, timeout, store):
        nonlocal index
        check_key = key + f"/check-{index}"
        index += 1
        if cancelled():
            raise VerificationError("repair cancelled before check")
        # Checks are commands with caller authority, so interruptions must not
        # cause them to be silently repeated as if they were pure reads.
        def capture(allowance):
            out = run_capture(workspace, argv, cwd=cwd, timeout=allowance, store=store,
                              output_bytes=capture_bytes, cancelled=cancelled, env=env)
            return {"manifest_id": out.manifest_id, "manifest": out.manifest}
        from ctx.edit_verification import applied_files
        applied = read_evidence(store, edit_receipt, "ctx.edit-receipt/v1")
        inputs = file_digests(workspace, [*applied_files(workspace, applied), *witnesses])
        value = perform(check_key, "repair.check", {"argv": argv, "cwd": cwd, "inputs": inputs,
                        "environmentHash": hashlib.sha256(canonical_json(env)).hexdigest()},
                        capture, kind="mutate", timeout=timeout)
        return CaptureResult(**value)
    proof = verify_edit(ws, store, edit_receipt, [*targeted, *regression], witnesses=witnesses, runner=runner)
    if proof["outcome"] == "passed":
        validate_verification(ws, store, proof["verificationRef"])
    result = {"schema": "ctx.repair-verification/v1", "failureRef": "run:" + failure_id,
        "editReceipt": edit_receipt, "verificationRef": proof["verificationRef"],
        "outcome": proof["outcome"], "inputs": proof["inputs"],
        "targetedChecks": len(targeted), "regressionChecks": len(regression),
        "checks": [{**c, "role": "targeted" if n < len(targeted) else "regression"}
                   for n, c in enumerate(proof["checks"])]}
    return {**result, "repairRef": publish(store, result)}
