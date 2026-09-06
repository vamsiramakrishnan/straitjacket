"""Checks tied to exact applied bytes. Application and correctness stay separate.

Commands are caller-selected local execution, never inferred from tool output.
Receipts prove which check ran against which files, not that the check is a
complete specification. The local artifact store is evidence, not a sandbox.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ctx.execution import run_capture
from ctx.store import canonical_json


class VerificationError(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    kind: str
    argv: tuple[str, ...]
    timeout: float = 60.0


def read_evidence(store, ref: str, schema: str) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("blob:"):
        raise VerificationError("expected a blob: evidence address")
    data = store.get_blob(ref[5:])
    # Store resolves short ids. Verify bytes too, rather than trusting a
    # mutable copy of a receipt supplied by the worker.
    full = store.resolve_id(ref[5:], kinds=("blob",))
    if hashlib.sha256(data).hexdigest() != full:
        raise VerificationError("evidence content identity mismatch")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise VerificationError(f"expected {schema}")
    return value


def file_digests(ws, paths) -> dict[str, str]:
    result = {}
    for rel in sorted(set(paths)):
        path = ws.confine(rel, must_exist=True)
        if ws.is_ignored(ws.relativize(path)):
            raise VerificationError("check input excluded by policy")
        h = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                h.update(chunk)
        result[rel] = "sha256:" + h.hexdigest()
    return result


def applied_files(ws, receipt) -> dict[str, str]:
    if receipt.get("workspaceId") != ws.workspace_id or receipt.get("operation") != "apply":
        raise VerificationError("apply receipt belongs to a different workspace or operation")
    files = receipt.get("files")
    if receipt.get("outcome") != "applied" or not isinstance(files, list) or not files:
        raise VerificationError("an applied edit receipt is required")
    if not any(f["beforeSha256"] != f["afterSha256"] for f in files):
        raise VerificationError("no-op edit does not establish progress")
    expected = {f["path"]: f["afterSha256"] for f in files}
    if file_digests(ws, expected) != expected:
        raise VerificationError("applied bytes changed; verify the current edit instead")
    return expected


def verify_edit(ws, store, receipt_ref: str, checks: list[Check], *, witnesses=()) -> dict:
    """Run a bounded set of explicit checks and persist the resulting proof."""
    if not checks or len(checks) > 8:
        raise VerificationError("provide between one and eight checks")
    for check in checks:
        if check.kind not in {"syntax", "types", "behavior"} or not check.argv:
            raise VerificationError("check needs a kind and nonempty argv")
        if not 0 < check.timeout <= 600:
            raise VerificationError("check timeout must be in (0, 600] seconds")
    receipt = read_evidence(store, receipt_ref, "ctx.edit-receipt/v1")
    expected = applied_files(ws, receipt)
    inputs = file_digests(ws, [*expected, *witnesses])
    rows = []
    outcome = "passed"
    for check in checks:
        if file_digests(ws, inputs) != inputs:
            outcome = "stale"
            break
        capture = run_capture(ws, list(check.argv), timeout=check.timeout, store=store)
        result = capture.manifest["result"]
        rows.append({"kind": check.kind, "argv": list(check.argv),
                     "runRef": "run:" + capture.manifest_id,
                     "passed": result["exitCode"] == 0 and not result["timedOut"]})
        try:
            unchanged = file_digests(ws, inputs) == inputs
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            outcome = "stale"
            break
        if not rows[-1]["passed"]:
            outcome = "failed"
            break
    proof = {"schema": "ctx.edit-verification/v1", "workspaceId": ws.workspace_id,
             "editReceipt": receipt_ref, "inputs": inputs, "checks": rows,
             "outcome": outcome, "requestedChecks": len(checks)}
    return {**proof, "verificationRef": "blob:" + store.put_blob(canonical_json(proof))}


def validate_verification(ws, store, ref: str) -> dict:
    """Recheck a proof at handoff time, including run outcomes and live bytes."""
    proof = read_evidence(store, ref, "ctx.edit-verification/v1")
    if proof.get("workspaceId") != ws.workspace_id or proof.get("outcome") != "passed":
        raise VerificationError("verification is not a passing proof for this workspace")
    receipt = read_evidence(store, proof["editReceipt"], "ctx.edit-receipt/v1")
    expected = applied_files(ws, receipt)
    for item in receipt["files"]:
        diagnostic = item.get("diagnostics", {})
        if diagnostic.get("outcome") in {"issues", "stale"}:
            raise VerificationError("edit diagnostics are not clean/current")
        if diagnostic.get("receiptId"):
            from ctx.post_edit_diagnostics import load_receipt
            observed = load_receipt(ws.root, diagnostic["receiptId"])
            if (observed.get("outcome") != diagnostic.get("outcome")
                    or "sha256:" + str(observed.get("postEditDocumentDigest")) != item["afterSha256"]):
                raise VerificationError("diagnostic receipt does not describe the applied bytes")
    inputs = proof["inputs"]
    if not inputs or any(inputs.get(k) != v for k, v in expected.items()):
        raise VerificationError("verification does not cover the applied bytes")
    if file_digests(ws, inputs) != inputs:
        raise VerificationError("verification inputs are stale")
    checks = proof.get("checks", [])
    if len(checks) != proof.get("requestedChecks") or not checks:
        raise VerificationError("verification checks are incomplete")
    if not any(c.get("kind") == "behavior" for c in checks):
        raise VerificationError("handoff requires an explicit behavioral check")
    for check in checks:
        run = store.get_manifest(check["runRef"].removeprefix("run:"))
        if (run.get("workspaceId") != ws.workspace_id or run.get("argv") != check["argv"]
                or run["result"]["exitCode"] != 0 or run["result"]["timedOut"]):
            raise VerificationError("verification run failed or does not match the check")
    return proof
