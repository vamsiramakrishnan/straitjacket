"""Addressable, fail-closed edit transactions.

The retrieval layer's short content anchors are excellent working addresses,
but a mutation needs a stronger contract than "this looks like the span I
read".  This module turns one or more anchored spans into a sealed plan:

``plan``
    Resolve each ``A:B@anchor`` against the current file, snapshot the complete
    source file, and record a full SHA-256 digest of the exact target bytes.

``preview``
    Re-resolve every target against the current worktree and publish the full
    diff as an immutable blob.  No source file is written.

``apply``
    Preflight every target first, then replace all files.  A target may move,
    but it may not change; zero or multiple byte-identical candidates refuse.
    Multiple edits in one file must relocate by the same offset and may not
    overlap.  The plan is compare-and-swap, not fuzzy patching.

Plans contain replacement text and should be handled like source code.  The
receipts deliberately contain only paths, spans, sizes, and digests.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctx import anchors
from ctx.execution import snapshot_file
from ctx.store import Store, canonical_json
from ctx.workspace import Workspace, WorkspaceError

REQUEST_SCHEMA = "ctx.edit-request/v1"
PLAN_SCHEMA = "ctx.edit-plan/v1"
RECEIPT_SCHEMA = "ctx.edit-receipt/v1"
MAX_EDITS = 128


class EditTransactionError(Exception):
    """A refusal, optionally carrying the safe receipt that explains it."""

    def __init__(self, message: str, *, receipt: dict[str, Any] | None = None, code: str = "invalid_request"):
        self.code = code
        super().__init__(message)
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class _ResolvedEdit:
    plan: dict[str, Any]
    start: int
    end: int
    replacement: bytes


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    path: Path
    rel: str
    before: bytes
    after: bytes
    mode: int
    edits: tuple[_ResolvedEdit, ...]


def _sha(data: bytes, *, domain: bytes = b"") -> str:
    return "sha256:" + hashlib.sha256(domain + data).hexdigest()


def _text_lines(data: bytes, path: str) -> tuple[list[str], list[bytes]]:
    if b"\x00" in data[:8192]:
        raise EditTransactionError(f"binary content is not editable by line span: {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise EditTransactionError(f"file is not valid UTF-8: {path} ({e})") from None
    logical = text.splitlines()
    pieces = [part.encode("utf-8") for part in text.splitlines(keepends=True)]
    if len(logical) != len(pieces):  # defensive: the two stdlib modes must agree
        raise EditTransactionError(f"could not form stable line spans for {path}")
    return logical, pieces


def _span_bytes(pieces: list[bytes], start: int, span_len: int) -> bytes | None:
    window = pieces[start - 1 : start - 1 + span_len]
    if len(window) != span_len:
        return None
    return b"".join(window)


def _candidate_count(total: int, span_len: int) -> int:
    return max(0, total - span_len + 1)


def _unique_anchor_start(lines: list[str], want: str, span_len: int, near: int) -> int:
    count = _candidate_count(len(lines), span_len)
    if count > anchors.MAX_RELOCATION_CANDIDATES:
        raise EditTransactionError(
            f"anchor relocation needs {count} candidates, above the safe cap "
            f"of {anchors.MAX_RELOCATION_CANDIDATES}; narrow or refresh the span"
        )
    matches = [
        start
        for start in range(1, count + 1)
        if anchors.anchor(lines[start - 1 : start - 1 + span_len]) == want
    ]
    if not matches:
        raise EditTransactionError(f"anchor @{want} is lost; refresh the address")
    if len(matches) > 1:
        raise EditTransactionError(
            f"anchor @{want} is ambiguous at {len(matches)} locations; use a larger span"
        )
    return matches[0]


def _unique_digest_start(
    pieces: list[bytes], want: str, span_len: int, near: int
) -> int:
    current = _span_bytes(pieces, near, span_len)
    if current is not None and _sha(current, domain=b"ctx.edit.span/v1\x00") == want:
        return near
    count = _candidate_count(len(pieces), span_len)
    if count > anchors.MAX_RELOCATION_CANDIDATES:
        raise EditTransactionError(
            f"stale target relocation needs {count} candidates, above the safe cap "
            f"of {anchors.MAX_RELOCATION_CANDIDATES}; make a fresh plan"
        )
    matches = []
    for start in range(1, count + 1):
        raw = _span_bytes(pieces, start, span_len)
        if raw is not None and _sha(raw, domain=b"ctx.edit.span/v1\x00") == want:
            matches.append(start)
            if len(matches) > 1:
                break
    if not matches:
        raise EditTransactionError("planned target bytes changed or disappeared; make a fresh plan", code="stale_target")
    if len(matches) > 1:
        raise EditTransactionError("planned target is now ambiguous; refusing to choose a copy", code="ambiguous_target")
    return matches[0]


def _plan_body(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "id"}


def _seal(plan: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(plan)
    sealed["id"] = _sha(canonical_json(_plan_body(sealed)), domain=b"ctx.edit.plan/v1\x00")
    return sealed


def validate_plan(ws: Workspace, plan: dict[str, Any]) -> None:
    if plan.get("schema") != PLAN_SCHEMA:
        raise EditTransactionError(f"expected {PLAN_SCHEMA}")
    if plan.get("workspaceId") != ws.workspace_id:
        raise EditTransactionError("plan belongs to a different workspace")
    expect = _sha(canonical_json(_plan_body(plan)), domain=b"ctx.edit.plan/v1\x00")
    if plan.get("id") != expect:
        raise EditTransactionError("plan integrity check failed; regenerate it")
    edits = plan.get("edits")
    if not isinstance(edits, list) or not edits:
        raise EditTransactionError("plan has no edits")


def create_edit_plan(
    ws: Workspace, store: Store, request: dict[str, Any]
) -> dict[str, Any]:
    """Resolve an edit request and return a sealed, serializable plan."""
    if request.get("schema") != REQUEST_SCHEMA:
        raise EditTransactionError(f"expected {REQUEST_SCHEMA}")
    requested = request.get("edits")
    if not isinstance(requested, list) or not requested:
        raise EditTransactionError("request must contain a non-empty edits list")
    if len(requested) > MAX_EDITS:
        raise EditTransactionError(f"request exceeds the {MAX_EDITS}-edit transaction cap")

    cached: dict[str, tuple[bytes, list[str], list[bytes], dict[str, Any]]] = {}
    planned: list[dict[str, Any]] = []
    for index, item in enumerate(requested):
        if not isinstance(item, dict):
            raise EditTransactionError(f"edit {index} must be an object")
        path_value = item.get("path")
        span_value = item.get("span")
        replacement = item.get("replacement")
        if not isinstance(path_value, str) or not path_value:
            raise EditTransactionError(f"edit {index} needs a path")
        if path_value.startswith("repo:"):
            path_value = path_value[5:]
        if not isinstance(span_value, str):
            raise EditTransactionError(f"edit {index} needs an anchored span")
        if not isinstance(replacement, str):
            raise EditTransactionError(f"edit {index} replacement must be text")
        try:
            start, end, short_anchor = anchors.parse_span(span_value)
        except ValueError as e:
            raise EditTransactionError(str(e)) from None
        if short_anchor is None:
            raise EditTransactionError(
                f"edit {index} span is unanchored; retrieve it with ctx get first"
            )

        full = ws.confine(path_value, must_exist=True)
        if not full.is_file():
            raise EditTransactionError(f"edit target is not a file: {path_value}")
        rel = ws.relativize_as_asked(path_value)
        if rel not in cached:
            snap = snapshot_file(store, ws, path_value)
            data = store.get_blob(snap["blob"].removeprefix("sha256:"))
            lines, pieces = _text_lines(data, rel)
            cached[rel] = (data, lines, pieces, snap)
        data, lines, pieces, snap = cached[rel]
        span_len = end - start + 1
        stated = lines[start - 1 : end] if 1 <= start <= len(lines) else []
        if len(stated) == span_len and anchors.anchor(stated) == short_anchor:
            resolved = start
        else:
            resolved = _unique_anchor_start(lines, short_anchor, span_len, start)
        target = _span_bytes(pieces, resolved, span_len)
        if target is None:  # guarded above, kept total at the byte boundary
            raise EditTransactionError(f"span selects nothing in {rel}")
        planned.append(
            {
                "path": rel,
                "requestedSpan": span_value,
                "plannedSpan": anchors.format_span(
                    resolved, resolved + span_len - 1, short_anchor
                ),
                "spanLength": span_len,
                "beforeSha256": _sha(target, domain=b"ctx.edit.span/v1\x00"),
                "replacement": replacement,
                "replacementSha256": _sha(
                    replacement.encode("utf-8"), domain=b"ctx.edit.replacement/v1\x00"
                ),
                "sourceSnapshot": "snapshot:" + str(snap["id"]).removeprefix("sha256:"),
                "sourceBlob": snap["blob"],
                "sourceFileSha256": _sha(data),
            }
        )

    plan = {
        "schema": PLAN_SCHEMA,
        "workspaceId": ws.workspace_id,
        "edits": planned,
    }
    return _seal(plan)


def _refusal(plan: dict[str, Any], operation: str, reason: str, code: str = "invalid_plan") -> EditTransactionError:
    return EditTransactionError(
        reason, code=code,
        receipt={
            "schema": RECEIPT_SCHEMA,
            "operation": operation,
            "planId": plan.get("id"),
            "outcome": "refused",
            "reason": reason,
            "code": code,
            "retryable": code in {"stale_target", "ambiguous_target", "source_changed"},
            "recovery": [
                {"ref": "repo:" + str(e.get("path", "")),
                 "selector": {"lines": str(e.get("plannedSpan", "")).split("@")[0]},
                 "action": "read_current_context_then_replan"}
                for e in plan.get("edits", [])[:MAX_EDITS] if isinstance(e, dict)
            ],
            "files": [],
        },
    )


def _prepare(ws: Workspace, plan: dict[str, Any], operation: str) -> list[_PreparedFile]:
    prepared: list[_PreparedFile] = []
    try:
        validate_plan(ws, plan)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edit in plan["edits"]:
            grouped.setdefault(str(edit["path"]), []).append(edit)
        for rel in sorted(grouped):
            full = ws.confine(rel, must_exist=True)
            before = full.read_bytes()
            _, pieces = _text_lines(before, rel)
            resolved: list[_ResolvedEdit] = []
            shifts: set[int] = set()
            for edit in grouped[rel]:
                pstart, pend, _ = anchors.parse_span(str(edit["plannedSpan"]))
                span_len = int(edit["spanLength"])
                if pend - pstart + 1 != span_len:
                    raise EditTransactionError("plan span length is internally inconsistent")
                start = _unique_digest_start(
                    pieces, str(edit["beforeSha256"]), span_len, pstart
                )
                shifts.add(start - pstart)
                resolved.append(
                    _ResolvedEdit(
                        edit,
                        start,
                        start + span_len - 1,
                        str(edit["replacement"]).encode("utf-8"),
                    )
                )
            if len(shifts) > 1:
                raise EditTransactionError(
                    f"edits in {rel} relocated by inconsistent offsets; make a fresh plan"
                )
            ordered = sorted(resolved, key=lambda edit: (edit.start, edit.end))
            for left, right in zip(ordered, ordered[1:]):
                if right.start <= left.end:
                    raise EditTransactionError(f"planned edits overlap after resolution in {rel}", code="overlapping_edits")
            after_parts = list(pieces)
            for edit in reversed(ordered):
                after_parts[edit.start - 1 : edit.end] = [edit.replacement]
            prepared.append(
                _PreparedFile(
                    full,
                    rel,
                    before,
                    b"".join(after_parts),
                    full.stat().st_mode,
                    tuple(ordered),
                )
            )
    except (KeyError, TypeError, ValueError, OSError, WorkspaceError, EditTransactionError) as e:
        reason = str(e) if isinstance(e, EditTransactionError) else f"invalid plan: {e}"
        raise _refusal(plan, operation, reason, e.code if isinstance(e, EditTransactionError) else "invalid_plan") from None
    return prepared


def _file_receipt(
    item: _PreparedFile, diagnostic: dict[str, Any] | None = None
) -> dict[str, Any]:
    receipt = {
        "path": item.rel,
        "beforeSha256": _sha(item.before),
        "afterSha256": _sha(item.after),
        "bytesBefore": len(item.before),
        "bytesAfter": len(item.after),
        "edits": [
            {
                "plannedSpan": edit.plan["plannedSpan"],
                "resolvedSpan": f"{edit.start}:{edit.end}",
                "relocated": edit.start
                != anchors.parse_span(str(edit.plan["plannedSpan"]))[0],
                "replacementSha256": edit.plan["replacementSha256"],
            }
            for edit in item.edits
        ],
    }
    if diagnostic is not None:
        receipt["diagnostics"] = {
            "receiptId": diagnostic.get("receiptId"),
            "outcome": diagnostic.get("outcome", "unavailable"),
        }
    return receipt


def preview_edit_plan(
    ws: Workspace, store: Store, plan: dict[str, Any]
) -> dict[str, Any]:
    """Preflight a plan and publish its complete current diff as a blob."""
    prepared = _prepare(ws, plan, "preview")
    diffs: list[str] = []
    try:
        for item in prepared:
            before = item.before.decode("utf-8").splitlines(keepends=True)
            after = item.after.decode("utf-8").splitlines(keepends=True)
            diffs.extend(
                difflib.unified_diff(
                    before, after, fromfile=f"a/{item.rel}", tofile=f"b/{item.rel}"
                )
            )
    except UnicodeDecodeError as e:  # should already have been rejected by _text_lines
        raise _refusal(plan, "preview", f"diff rendering failed: {e}") from None
    patch = "".join(diffs).encode("utf-8")
    blob = store.put_blob(patch)
    return {
        "schema": RECEIPT_SCHEMA,
        "operation": "preview",
        "planId": plan["id"],
        "outcome": "ready",
        "patch": f"blob:{blob}",
        "files": [_file_receipt(item) for item in prepared],
    }


def _stage(path: Path, data: bytes, mode: int) -> Path:
    fd, name = tempfile.mkstemp(prefix=".ctx-edit-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def apply_edit_plan(ws: Workspace, plan: dict[str, Any], *, attempt_key: str | None = None) -> dict[str, Any]:
    """Compare-and-swap every planned file, or refuse before source writes."""
    prepared = _prepare(ws, plan, "apply")
    # Capture these immediately before staging/writing.  The diagnostic layer
    # owns freshness evidence; this transaction owns write ordering.
    from ctx.post_edit_diagnostics import capture_baseline, verify_post_edit

    baselines = {
        item.rel: capture_baseline(ws.root, item.rel)
        for item in prepared
    }
    staged: dict[str, Path] = {}
    committed: list[_PreparedFile] = []
    try:
        for item in prepared:
            staged[item.rel] = _stage(item.path, item.after, item.mode)
        # Validate the entire set after all outputs are staged: no commit starts
        # with a target already known stale.
        for item in prepared:
            if item.path.read_bytes() != item.before:
                raise EditTransactionError(
                    f"{item.rel} changed during preflight; no files were written"
                )
        for item in prepared:
            # Narrow the unavoidable cooperative-CAS race further by checking
            # again directly before this file's rename.  An editor that ignores
            # the protocol can still race between this read and os.replace.
            if item.path.read_bytes() != item.before:
                raise EditTransactionError(f"{item.rel} changed during commit")
            # Rename first, forget second: popping before the rename left a
            # failed rename's temp file tracked by nothing.
            os.replace(staged[item.rel], item.path)
            del staged[item.rel]
            committed.append(item)
    except Exception as e:
        rollback_failed: list[str] = []
        for item in reversed(committed):
            try:
                # Do not overwrite a third party's post-commit change while
                # trying to recover from an internal multi-file failure.
                if item.path.read_bytes() == item.after:
                    restore = _stage(item.path, item.before, item.mode)
                    try:
                        os.replace(restore, item.path)
                    except Exception:
                        # _stage only unlinks on its own failure; a failed
                        # rename here left its temp file beside the target.
                        restore.unlink(missing_ok=True)
                        raise
                else:
                    rollback_failed.append(item.rel)
            except Exception:
                rollback_failed.append(item.rel)
        for temp in staged.values():
            temp.unlink(missing_ok=True)
        reason = str(e)
        if rollback_failed:
            reason += "; rollback could not safely restore: " + ", ".join(rollback_failed)
        raise _refusal(plan, "apply", reason) from None
    diagnostics: dict[str, dict[str, Any]] = {}
    for item in prepared:
        try:
            diagnostics[item.rel] = verify_post_edit(
                ws.root, item.rel, baselines[item.rel], run_builtin=True, persist=True,
                expected_digest=_sha(item.after).removeprefix("sha256:"),
            )
        except Exception as e:
            # The source transaction has committed.  A diagnostic adapter
            # failure cannot honestly turn that into an apply refusal (nor can
            # it safely roll the user's change back), so disclose unavailable.
            diagnostics[item.rel] = {
                "receiptId": None,
                "outcome": "unavailable",
                "reason": f"diagnostic_error:{type(e).__name__}",
            }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "workspaceId": ws.workspace_id,
        "operation": "apply",
        "planId": plan["id"],
        "outcome": "applied",
        "files": [_file_receipt(item, diagnostics[item.rel]) for item in prepared],
    }
    key = attempt_key if attempt_key is not None else os.environ.get("CTX_EDIT_ATTEMPT")
    if key:
        receipt["attemptKey"] = key
    # The write already succeeded. Evidence storage failure must stay distinct
    # from apply failure, and makes the result ineligible for prewalk.
    try:
        receipt_store = Store(ws.workspace_id)
        try:
            receipt["receiptRef"] = "blob:" + receipt_store.put_blob(canonical_json(receipt))
        finally:
            receipt_store.close()
    except Exception as exc:
        receipt["evidenceError"] = type(exc).__name__
    return receipt


def replace_span(ws: Workspace, store: Store, ref: str, span: str,
                 replacement: str, *, apply: bool = False, attempt_key: str | None = None) -> dict[str, Any]:
    """Plan and preview/apply one anchored replacement without a JSON file.

    This is a local SDK mutation, subject to the same authority as ctx run.
    It does not expose writes through the retrieval MCP server.
    """
    plan = create_edit_plan(ws, store, {
        "schema": REQUEST_SCHEMA,
        "edits": [{"path": ref, "span": span, "replacement": replacement}],
    })
    plan_ref = "blob:" + store.put_blob(canonical_json(plan))
    result = apply_edit_plan(ws, plan, attempt_key=attempt_key) if apply else preview_edit_plan(ws, store, plan)
    return {**result, "planRef": plan_ref}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise EditTransactionError(f"cannot read JSON from {path.name}: {e}") from None
    if not isinstance(value, dict):
        raise EditTransactionError(f"{path.name} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write a human-reviewable JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = _stage(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(), 0o100600)
    os.replace(staged, path)


__all__ = [
    "REQUEST_SCHEMA",
    "PLAN_SCHEMA",
    "RECEIPT_SCHEMA",
    "EditTransactionError",
    "create_edit_plan",
    "validate_plan",
    "preview_edit_plan",
    "apply_edit_plan",
    "load_json",
    "write_json",
]
