"""Opt-in, workspace-bound MCP edits using the existing transaction engine."""
from __future__ import annotations

import json
from pathlib import Path

TOOL_SCHEMA = {
    "name": "ctx_edit",
    "description": "Verified edits. plan seals anchored edits and previews a patch; apply uses its planRef. replace previews one anchored span (apply=true writes). rewrite previews a structural multi-file change; rewrite_apply uses that preview's receiptRef. No shell execution. Fetch full receipts/patches with ctx get.",
    "inputSchema": {
        "type": "object", "required": ["op"], "additionalProperties": False,
        "properties": {
            "op": {"enum": ["plan", "apply", "replace", "rewrite", "rewrite_apply"]},
            "workspace": {"type": "string", "description": "Must equal the server's configured workspace"},
            "request": {"type": "object", "description": "ctx.edit-request/v1: schema plus edits [{path, span:'A:B@anchor', replacement}]"},
            "planRef": {"type": "string"}, "receiptRef": {"type": "string"},
            "ref": {"type": "string"}, "span": {"type": "string"},
            "replacement": {"type": "string"}, "pattern": {"type": "string"},
            "language": {"type": "string"}, "glob": {"type": "string"},
            "apply": {"type": "boolean", "default": False},
        },
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
}


def dispatch(args: dict, root: str) -> str:
    from ctx.mcp import _resolve_cached
    from ctx.edit_transactions import create_edit_plan, preview_edit_plan, apply_edit_plan, replace_span
    from ctx.edit_verification import read_evidence
    from ctx.store import canonical_json
    from ctx.textutil import sanitize_for_model

    if not isinstance(args, dict):
        raise ValueError("ctx_edit arguments must be an object")
    ws, store = _resolve_cached(root)
    if args.get("workspace") and Path(args["workspace"]).resolve() != ws.root.resolve():
        raise ValueError("ctx_edit cannot change the server's workspace")
    def required(name):
        value = args.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"ctx_edit {args.get('op')} requires {name}")
        return value
    op = args.get("op")
    if op == "plan":
        plan = create_edit_plan(ws, store, args.get("request") or {})
        plan_ref = "blob:" + store.put_blob(canonical_json(plan))
        result = {**preview_edit_plan(ws, store, plan), "planRef": plan_ref}
    elif op == "apply":
        plan = read_evidence(store, required("planRef"), "ctx.edit-plan/v1")
        result = apply_edit_plan(ws, plan)
    elif op == "replace":
        replacement = args.get("replacement")
        if not isinstance(replacement, str) or type(args.get("apply", False)) is not bool:
            raise ValueError("replacement must be text and apply must be boolean")
        result = replace_span(ws, store, required("ref"), required("span"), replacement,
                              apply=args.get("apply", False))
    elif op == "rewrite":
        from ctx.astgrep import rewrite_preview
        replacement = args.get("replacement")
        if not isinstance(replacement, str):
            raise ValueError("replacement must be text")
        rows, meta = rewrite_preview(ws, store, required("pattern"), replacement,
                                     language=args.get("language"), glob=args.get("glob"))
        result = {"schema": "ctx.mcp-rewrite-preview/v1", "workspace": str(ws.root),
                  "outcome": "preview", "files": rows, **meta}
    elif op == "rewrite_apply":
        from ctx.astgrep import rewrite_apply
        preview = read_evidence(store, required("receiptRef"), "ctx.mcp-rewrite-preview/v1")
        if preview.get("workspace") != str(ws.root) or preview.get("preview_omitted", 0):
            raise ValueError("rewrite apply requires a complete preview from this workspace; narrow the pattern/glob")
        rows, meta = rewrite_apply(ws, store, preview.get("patch_blob"), preview.get("generation"))
        result = {"outcome": "applied", "files": rows, **meta}
    else:
        raise ValueError(f"unknown ctx_edit operation: {op}")
    raw = canonical_json(result)
    receipt = "blob:" + store.put_blob(raw)
    text, _ = sanitize_for_model(raw.decode(), ws.config.redaction)
    limit = min(4096, ws.config.budgets.result_tokens * 4)
    if len(text.encode()) > limit:
        result = {"outcome": result.get("outcome"), "receiptRef": receipt,
                  "omittedBytes": len(raw), "next": f"ctx get {receipt}",
                  **{k: result[k] for k in ("planRef", "patch", "patch_blob") if k in result}}
    else:
        result = {**json.loads(text), "receiptRef": receipt}
    return json.dumps(result, separators=(",", ":"))
