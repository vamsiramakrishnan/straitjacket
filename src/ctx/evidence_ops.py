"""Registered evidence operations used by plans, SDKs and adaptive policies."""
from __future__ import annotations

from ctx.plan_ops import payload, register_op, OpError
from ctx.evidence_access import EvidenceAccess


def _access(pc):
    return pc.evidence or EvidenceAccess()


def discover(pc, args, inp):
    names = _access(pc).files(pc.ws)
    return payload("files", [{"path": p, "ref": "repo:" + p} for p in names],
                   meta={"engine": "workspace-policy", "selected_files": len(names),
                         "selection_complete": None})


def read(pc, args, inp):
    item = _access(pc).read(pc.ws, pc.store, args["ref"], args["start"], args["end"])
    return payload("text", [item], meta={"engine": "immutable-source-read"},
                   artifacts={"source": item["raw_ref"], "view": item["ref"]})


def search(pc, args, inp):
    from ctx.retrieval import search as retrieve_search
    access = _access(pc)
    parsed = access.check(pc.ws, args["ref"])
    if not isinstance(args["query"], str) or not args["query"] or len(args["query"].encode()) > 2000:
        raise OpError("provide a bounded literal query")
    if parsed.kind == "repo":
        names = pc.ws.list_files(parsed.path or ".")
        if len(names) > access.max_files or sum(pc.ws.confine(p).stat().st_size for p in names) > access.source_bytes:
            raise OpError("search exceeds the evidence allowance; narrow its ref")
    else:
        from ctx.semantic.evidence import _source
        _source(pc.ws, pc.store, args["ref"], access.source_bytes)
    # Reuse the retrieval engine, including its matched-region handles and
    # explicit coverage. Literal patterns avoid an unbounded model regex.
    text = retrieve_search(pc.store, pc.ws, args["ref"], [args["query"]], fixed=True,
                           max_matches=50)
    ref = "blob:" + pc.store.put_blob(text.encode())
    return payload("text", [{"text": text, "ref": ref}],
                   meta={"engine": "ctx.search", "selection_complete": None}, artifacts={"result": ref})


def semantic_map(pc, args, inp):
    from ctx.semantic import prepare, run
    if pc.runtime is None:
        raise OpError("semantic.map requires an explicit TaskRuntime")
    access = _access(pc)
    if not access.worker_spec:
        raise OpError("semantic.map needs an explicitly configured worker")
    if not isinstance(args["sources"], list) or not 1 <= len(args["sources"]) <= 32:
        raise OpError("select 1..32 evidence sources")
    for ref in args["sources"]:
        access.check(pc.ws, ref)
    limits = pc.runtime.limits
    request = {"question": args["question"], "sources": args["sources"], "worker": access.worker_spec,
        "sample": pc.runtime.task_id + "/" + str(args.get("sample", "default")),
        "limits": {"source_bytes": access.source_bytes, "partition_bytes": access.read_bytes,
                   "wall_seconds": limits.wall_seconds, "call_seconds": limits.call_seconds,
                   "max_calls": min(1024, limits.max_calls), "max_tokens": limits.max_tokens,
                   "max_cost_usd": limits.max_cost_usd, "reserve_cost_usd": limits.reserve_cost_usd,
                   "max_output_tokens": limits.max_output_tokens, "response_bytes": limits.response_bytes}}
    plan_ref = prepare(pc.ws, pc.store, request)
    report_ref, report = run(pc.ws, pc.store, plan_ref, worker=pc.worker, runtime=pc.runtime,
                            retry_failed=pc.runtime.retry_failed)
    return payload("records", report["findings"], meta={"engine": "semantic-map",
        "status": report["status"], "unresolved": report["unresolved"],
        "coverage": report["coverage"], "epistemic_status": "model_inference"},
        artifacts={"plan": plan_ref, "report": report_ref})


def probe(pc, args, inp):
    from ctx.execution import run_capture
    access = _access(pc)
    if pc.runtime is None or args["name"] not in access.probes:
        raise OpError("probe.run requires a caller-configured command and TaskRuntime")
    result = run_capture(pc.ws, access.probes[args["name"]], store=pc.store, timeout=pc.timeout,
        runtime=pc.runtime, operation_key="probe/" + args["sample"])
    ref = "run:" + result.manifest_id
    return payload("records", [result.manifest["result"]], meta={"engine": "run-capture"},
                   artifacts={"stdout": ref + "#stdout", "stderr": ref + "#stderr"})


def install():
    register_op("evidence.discover", discover, input_kinds=(), output_kind="files",
                doc="enumerate policy-eligible files in an EvidenceAccess scope")
    register_op("evidence.read", read, input_kinds=(), output_kind="text",
                doc="freeze and read an exact source span (ref, start, end)",
                check_args=lambda a: None if set(a) == {"ref", "start", "end"} else "provide ref, start, end")
    register_op("evidence.search", search, input_kinds=(), output_kind="text",
                doc="literal search through ctx search (ref, query)",
                check_args=lambda a: None if set(a) == {"ref", "query"} else "provide ref, query")
    register_op("semantic.map", semantic_map, input_kinds=(), output_kind="records",
                klass="execute", composite=True,
                doc="explicit model map over selected evidence; requires TaskRuntime and worker",
                check_args=lambda a: None if set(a) <= {"sources", "question", "sample"} and {"sources", "question"} <= set(a) else "provide sources and question")
    register_op("probe.run", probe, input_kinds=(), output_kind="records", klass="execute", composite=True,
                doc="capture a caller-configured named command; requires TaskRuntime",
                check_args=lambda a: None if set(a) == {"name", "sample"} else "provide name and sample")
