"""Verified prewalk and a bounded, explicit checkpoint continuation protocol."""
from __future__ import annotations

import json
import re

from ctx.edit_verification import VerificationError, read_evidence, validate_verification
from ctx.store import canonical_json
from ctx.textutil import sanitize_for_model

SENTINEL = "CTX_PREWALK_HANDOFF"
STATE_PREFIX = "CTX_PREWALK_STATE "
SCHEMA = "ctx.prewalk-state/v1"
MAX_STATE_BYTES = 16000


def _state(state: dict) -> dict:
    if not isinstance(state, dict):
        raise VerificationError("handoff state must be an object")
    allowed = {"checklist", "hypotheses", "ruledOut", "evidence"}
    if set(state) - allowed:
        raise VerificationError("unknown handoff state field")
    checklist = state.get("checklist")
    if not isinstance(checklist, list) or not 1 <= len(checklist) <= 12:
        raise VerificationError("handoff needs 1..12 checklist items")
    ids = set()
    for item in checklist:
        if not isinstance(item, dict) or set(item) != {"id", "task", "validation", "status"}:
            raise VerificationError("each checklist item needs id, task, validation, status")
        if any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 500
               for k in item):
            raise VerificationError("checklist fields must be nonempty bounded text")
        if item["id"] in ids or item["status"] not in {"done", "pending"}:
            raise VerificationError("checklist ids must be unique and statuses done/pending")
        ids.add(item["id"])
    if {i["status"] for i in checklist} != {"done", "pending"}:
        raise VerificationError("handoff needs demonstrated progress and remaining work")
    for key in allowed - {"checklist"}:
        values = state.get(key, [])
        if not isinstance(values, list) or len(values) > 12:
            raise VerificationError(f"{key} must be a bounded list")
        if any(not isinstance(v, str) or len(v) > 500 for v in values):
            raise VerificationError(f"{key} entries must be bounded text")
    if len(canonical_json(state)) > MAX_STATE_BYTES:
        raise VerificationError("handoff state exceeds byte budget")
    return state


def create_handoff(ws, store, verification_ref: str, state: dict) -> dict:
    proof = validate_verification(ws, store, verification_ref)
    receipt = read_evidence(store, proof["editReceipt"], "ctx.edit-receipt/v1")
    key = receipt.get("attemptKey")
    if not key:
        raise VerificationError("edit is not bound to a prewalk attempt")
    body = {"schema": SCHEMA, "attemptKey": key, "verificationRef": verification_ref,
            "continuationMode": "checkpoint", "state": _state(state)}
    ref = "blob:" + store.put_blob(canonical_json(body))
    return {**body, "stateRef": ref, "signal": SENTINEL + "\n" + STATE_PREFIX + ref}


def requested(stdout: str) -> bool:
    return SENTINEL in stdout.splitlines()


def accept_handoff(ws, store, stdout: str, attempt_key: str) -> dict:
    refs = [line[len(STATE_PREFIX):] for line in stdout.splitlines() if line.startswith(STATE_PREFIX)]
    if not requested(stdout) or len(refs) != 1 or not re.fullmatch(r"blob:[0-9a-f]{64}", refs[0]):
        raise VerificationError("handoff requires one exact signal and state address")
    if stdout.splitlines().count(SENTINEL) != 1:
        raise VerificationError("duplicate handoff signal")
    body = read_evidence(store, refs[0], SCHEMA)
    proof = validate_verification(ws, store, body["verificationRef"])
    receipt = read_evidence(store, proof["editReceipt"], "ctx.edit-receipt/v1")
    if body.get("attemptKey") != attempt_key or receipt.get("attemptKey") != attempt_key:
        raise VerificationError("handoff evidence belongs to another attempt")
    _state(body["state"])
    # Keep the entire small checklist in the initial continuation. Evidence is
    # addressed; replay is explicitly a new launch, not native session resume.
    projection = json.dumps({"stateRef": refs[0], "verificationRef": body["verificationRef"],
                             "continuationMode": "checkpoint", **body["state"]},
                            sort_keys=True, separators=(",", ":"))
    text, _ = sanitize_for_model(projection, ws.config.redaction)
    return {"stateRef": refs[0], "text": text, "proof": proof}
