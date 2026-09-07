"""Freeze explicit selections and partition by file, symbol, paragraph, line."""
from __future__ import annotations

import ast
import json

from ctx.refs import parse_ref
from ctx.store import Store, canonical_json
from ctx.textutil import _redaction_of, sanitize_for_model
from ctx.semantic.contract import PROMPT, RESPONSE_SCHEMA, SemanticError, identity, parse_json, request


def policy_id(ws) -> str:
    return identity(_redaction_of(ws.config.redaction))


def publish(store: Store, value: dict, *, evidence=()) -> str:
    """A readable blob plus a retention manifest rooting every evidence blob.

    The store's collector follows hashes inside manifests, not inside arbitrary
    JSON blobs. Put the transitive evidence here too, so a report remains usable
    when its older preparation/attempt manifests expire.
    """
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
    ref = "blob:" + store.put_blob(data)
    store.put_manifest({"schema": "ctx.semantic.artifact/v1", "artifact": ref,
                        "evidence": list(evidence), "document": value}, kind="semantic")
    return ref


def read_document(store: Store, ref: str, *, cap=8 * 1024 * 1024) -> dict:
    parsed = parse_ref(ref)
    if parsed.kind != "blob" or parsed.workspace_alias:
        raise SemanticError("semantic plans and reports use local blob: handles")
    full = store.resolve_id(parsed.id or "", kinds=("blob",))
    with store.blob_path(full).open("rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise SemanticError("semantic document exceeds the read limit")
    value = parse_json(data)
    if not isinstance(value, dict):
        raise SemanticError("expected a semantic document")
    return value


def _source(ws, store, handle: str, remaining: int):
    ref = parse_ref(handle)
    if ref.workspace_alias:
        raise SemanticError("select evidence in one workspace per semantic map")
    if ref.kind == "repo":
        if not ref.path:
            raise SemanticError("select explicit files; repo-wide discovery belongs to the host")
        path = ws.confine(ref.path, must_exist=True)
        if not path.is_file() or any(ws.is_ignored(p) for p in (ref.path, ws.relativize(path))):
            raise SemanticError("selected file is not capturable under workspace policy")
    elif ref.kind in {"blob", "run", "snapshot"}:
        blob = ref.id or ""
        if ref.kind != "blob":
            manifest = store.get_manifest(blob)
            if ref.kind == "run":
                if ref.stream is None:
                    raise SemanticError("select #stdout or #stderr explicitly")
                blob = manifest["streams"][ref.stream]["blob"].removeprefix("sha256:")
            else:
                blob = manifest["blob"].removeprefix("sha256:")
        path = store.blob_path(store.resolve_id(blob, kinds=("blob",)))
    else:
        raise SemanticError("select repo files, blobs, snapshots, or explicit run streams")
    with path.open("rb") as fh:
        data = fh.read(remaining + 1)
    if len(data) > remaining:
        raise SemanticError("selected evidence exceeds source_bytes; narrow the selection or raise the limit")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise SemanticError("semantic evidence must be UTF-8 text") from exc
    if "\0" in text:
        raise SemanticError("binary evidence is not supported")
    view, redactions = sanitize_for_model(text, ws.config.redaction)
    return data, view.encode("utf-8"), redactions


def partition(data: bytes, ref: str, label: str, cap: int) -> list[dict]:
    """Nonoverlapping coverage; never discard an oversized tail or split a line."""
    parts = data.split(b"\n")
    lines = [line + b"\n" for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    starts = {1, len(lines) + 1}
    if label.endswith(".py"):
        try:
            tree = ast.parse(data.decode("utf-8"))
            starts.update(min([node.lineno, *[d.lineno for d in getattr(node, "decorator_list", [])]])
                          for node in tree.body)
        except (SyntaxError, RecursionError):
            pass  # malformed Python still has exact line/paragraph coverage
    starts.update(i + 2 for i, line in enumerate(lines) if not line.strip())
    boundaries = sorted(starts)
    units = [(a, b - 1) for a, b in zip(boundaries, boundaries[1:])]
    result, start, size, end = [], 1, 0, 0

    def emit():
        result.append({"ref": ref, "label": label, "start": start, "end": end, "bytes": size})

    for a, b in units:
        unit_size = sum(map(len, lines[a - 1:b]))
        if size and size + unit_size > cap:
            emit()
            start, size = a, 0
        for i in range(a, b + 1):
            length = len(lines[i - 1])
            if length > cap:
                raise SemanticError("one evidence line exceeds partition_bytes; raise the limit or select a structured view")
            if size and size + length > cap:
                emit()
                start, size = i, 0
            size += length
            end = i
    if size:
        emit()
    return result


def prepare(ws, store: Store, specification: dict) -> str:
    """Freeze evidence and configuration. No worker executes in this operation."""
    spec = request(specification)
    question, question_redactions = sanitize_for_model(spec["question"], ws.config.redaction)
    spec["question"] = question
    sources, partitions, total = [], [], 0
    for handle in dict.fromkeys(spec["sources"]):
        raw, view, redactions = _source(ws, store, handle, spec["limits"]["source_bytes"])
        raw_ref, view_ref = "blob:" + store.put_blob(raw), "blob:" + store.put_blob(view)
        # Repeated handles are deduplicated, but identical files at different
        # paths retain their labels: location can matter to the question.
        total += len(raw)
        if total > spec["limits"]["source_bytes"]:
            raise SemanticError("selected evidence exceeds source_bytes; narrow the selection or raise the limit")
        label, _ = sanitize_for_model(handle, ws.config.redaction)
        sources.append({"source": raw_ref, "ref": view_ref, "label": label,
                        "source_bytes": len(raw), "model_bytes": len(view),
                        "redactions": redactions, "transformed": raw != view})
        partitions.extend(partition(view, view_ref, label, spec["limits"]["partition_bytes"]))
        if len(partitions) > 1024:
            raise SemanticError("selection produces more than 1024 partitions")
    plan = {"schema": "ctx.semantic.plan/v1", "spec": spec,
            "prompt": PROMPT, "response_schema": RESPONSE_SCHEMA,
            "policy": policy_id(ws), "question_redactions": question_redactions,
            "sources": sources, "partitions": partitions}
    return publish(store, plan)


def load_plan(ws, store, handle):
    plan = read_document(store, handle)
    if (plan.get("schema") != "ctx.semantic.plan/v1" or plan.get("prompt") != PROMPT
            or plan.get("response_schema") != RESPONSE_SCHEMA):
        raise SemanticError("unsupported semantic plan or worker contract; prepare again")
    spec = request(plan.get("spec"))
    if plan.get("policy") != policy_id(ws):
        raise SemanticError("redaction policy changed; prepare the selection again")
    sources = plan.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 256:
        raise SemanticError("invalid frozen selection")
    # Verify frozen views and partition coverage before any paid launch. This
    # also detects hand-edited plans and artifacts missing after retention.
    expected, total = [], 0
    for source in sources:
        raw, view, redactions = _source(ws, store, source["source"], spec["limits"]["source_bytes"] - total)
        total += len(raw)
        if ("blob:" + identity_bytes(view) != source["ref"]
                or len(raw) != source["source_bytes"] or len(view) != source["model_bytes"]
                or redactions != source["redactions"]):
            raise SemanticError("frozen evidence does not match its model view")
        expected.extend(partition(view, source["ref"], source["label"], spec["limits"]["partition_bytes"]))
    if expected != plan.get("partitions") or len(expected) > 1024:
        raise SemanticError("frozen partition coverage is invalid")
    return plan


def identity_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def worker_request(store, plan, part):
    text = store.read_blob_lines(part["ref"][5:], part["start"], part["end"]).decode("utf-8")
    value = {"schema": "ctx.semantic.request/v1", "prompt": plan["prompt"],
             "question": plan["spec"]["question"], "worker": {
                 key: plan["spec"]["worker"][key] for key in ("identity", "model", "settings")},
             "max_output_tokens": plan["spec"]["limits"]["max_output_tokens"],
             "evidence": dict(part, text=text), "response_schema": plan["response_schema"]}
    return canonical_json(value)
