"""Investigation policy over registered evidence operations and TaskRuntime.

This module chooses transitions. Evidence acquisition, accounting, persistence,
process capture, edit transactions and verification are public shared services.
"""
from __future__ import annotations

from dataclasses import replace, asdict
import hashlib
import json

from ctx.controllers.investigation_contract import (request, decision, InvestigationError, within,
                                                    PROMPT, DECISION_SCHEMA)
from ctx.evidence_access import EvidenceAccess
from ctx.execution import run_capture
from ctx.edit_transactions import create_edit_plan, preview_edit_plan, apply_edit_plan
from ctx.edit_verification import Check, file_digests, verify_edit, validate_verification
from ctx.plan_exec import invoke_operation
from ctx.plan_ops import PlanContext, OPS, OpError
from ctx.semantic.contract import parse_json
from ctx.semantic.evidence import policy_id
from ctx.semantic.worker import CommandWorker
from ctx.store import canonical_json
from ctx.task_runtime import TaskRuntime, ExecutionLimits, ExecutionPaused, publish, read
from ctx.textutil import sanitize_for_model
from ctx.worktree_isolation import PersistentWorktree, clean_git_root, preflight_patch, apply_patches, WorktreePatch

OPERATORS = ("evidence.discover", "evidence.search", "evidence.read", "semantic.map", "probe.run")
VERSION = "ctx.investigation-policy/v1"


def _workspace_policy(ws):
    return hashlib.sha256(canonical_json({"config": asdict(ws.config), "ignore": ws.ignore_globs})).hexdigest()


def prepare(ws, store, specification):
    """Validate a task, pin the repository and checks, and return its task id."""
    spec = request(ws, specification)
    if not ws.git or not ws.git.head or not clean_git_root(ws.root):
        raise InvestigationError("prepare requires a clean Git root; commit or stash your changes first")
    access = EvidenceAccess(tuple(spec["scopes"]), max_files=spec["limits"]["max_files"])
    access.files(ws)
    witnesses = set()
    for path in spec["witnesses"]:
        entries = ws.list_files(path)
        if not entries:
            raise InvestigationError("a verification witness contains no eligible files")
        witnesses.update(entries)
    # Tool policy and discovery exclusions are immutable verification inputs.
    witnesses.update(p for p in ("ctx.toml", ".gitignore", ".ctxignore") if (ws.root / p).is_file())
    spec["witness_files"] = sorted(witnesses)
    spec["witness_hashes"] = file_digests(ws, witnesses)
    spec["question"] = sanitize_for_model(spec["question"], ws.config.redaction)[0]
    rt = TaskRuntime.create(ws, store, goal=spec["question"],
        limits=ExecutionLimits.from_dict(spec["limits"]),
        binding={"workspace": str(ws.root.resolve()), "base": ws.git.head,
                 "redaction": policy_id(ws), "policy": VERSION, "workspace_policy": _workspace_policy(ws)})
    with rt.active():
        rt.checkpoint("investigation.spec", spec)
        rt.checkpoint("investigation", {"status": "prepared", "phase": "baseline", "round": 0,
            "repairs": [], "observations": [], "allowed_refs": [], "read_views": [],
            "baseline": [], "fingerprint": None, "probe_count": 0})
    return rt.task_id


def _worker(spec, supplied, rt):
    if supplied is not None:
        return supplied
    if spec["endpoint"]:
        from ctx.acp import Endpoint
        from ctx.semantic.hosts import ACPWorker
        return ACPWorker(Endpoint(**spec["endpoint"]), cancelled=rt.cancelled)
    command = spec["worker"].get("command")
    if not command:
        raise InvestigationError("task needs a configured command driver or an SDK worker")
    return CommandWorker(command, cancelled=rt.cancelled)


def _view(value, config):
    if isinstance(value, str):
        return sanitize_for_model(value, config.redaction)[0]
    if isinstance(value, dict):
        return {k: _view(v, config) for k, v in value.items()}
    if isinstance(value, list):
        return [_view(v, config) for v in value]
    return value


def _context(rt, spec, state):
    observations = []
    for ref in state["observations"][-6:]:
        doc = read(rt.store, ref)
        rows = doc.get("rows", [])
        observations.append({"ref": ref, "op": doc.get("op"), "meta": doc.get("meta", {}),
                             "artifacts": doc.get("artifacts", {}), "rows": rows[:20],
                             "omitted_rows": doc.get("omitted", 0) + max(0, len(rows) - 20)})
    value = {"schema": "ctx.investigation.prompt/v1", "prompt": PROMPT, "question": spec["question"],
        "scopes": spec["scopes"],
        "targets": spec["targets"], "protected_files": spec["witness_files"],
        "worker": {k: spec["worker"][k] for k in ("identity", "model", "settings")},
        "max_output_tokens": rt.limits.max_output_tokens, "response_schema": DECISION_SCHEMA,
        "operations": {name: OPS[name].doc for name in OPERATORS},
        "probes": list(spec["probes"]), "phase": state["phase"], "round": state["round"],
        "baseline": state["baseline"],
        "observations": observations, "earlier_observations": state["observations"][:-6],
        "budget": rt.totals(), "repair_count": len(state["repairs"])}
    value = _view(value, rt.ws.config)
    while len(canonical_json(value)) > spec["limits"]["context_bytes"] and value["observations"]:
        omitted = value["observations"].pop(0)
        value["earlier_observations"].append(omitted["ref"])
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > spec["limits"]["context_bytes"]:
        raise ExecutionPaused("context_budget")
    return data


def _save(rt, state):
    # Keep the completed outcome if a later freshness check pauses inspection.
    # Restoring the exact witnessed bytes can then resume without new inference.
    if state["phase"] == "complete" and state["status"] not in {"paused", "interrupted"}:
        state["completion"] = {"status": state["status"], "stop_reason": state.get("stop_reason")}
    return rt.checkpoint("investigation", state)


def _record(rt, state, access, op, result):
    doc = dict(result, op=op)
    ref = publish(rt.store, doc)
    state["observations"].append(ref)
    access.allowed_refs.add(ref)
    access.allowed_refs.update(result.get("artifacts", {}).values())
    if op == "evidence.read":
        for item in result["rows"]:
            access.allowed_refs.add(item["ref"])
            state["read_views"].append(item)
    if op == "semantic.map":
        report = read(rt.store, result["artifacts"]["report"])
        for source in report["sources"]:
            access.allowed_refs.add(source["ref"])
        if report["status"] == "complete" and report["findings"]:
            state["analysis_fingerprint"] = state["fingerprint"]
    state["allowed_refs"] = sorted(access.allowed_refs)
    _save(rt, state)


def _assert_current(ws, lease, spec, state):
    if file_digests(ws, spec["witness_files"]) != spec["witness_hashes"]:
        raise ExecutionPaused("verification_inputs_changed")
    if state["fingerprint"] is not None and lease.fingerprint() != state["fingerprint"]:
        raise ExecutionPaused("worktree_changed")


def _capture(rt, ws, state, access, spec, lease, key, argv, timeout):
    _assert_current(ws, lease, spec, state)
    capture = run_capture(ws, argv, store=rt.store, timeout=timeout,
                          runtime=rt, operation_key=key)
    if rt.cancelled():
        raise ExecutionPaused("cancelled")
    _assert_current(ws, lease, spec, state)
    ref = "run:" + capture.manifest_id
    result = {"rows": [capture.manifest["result"]], "meta": {"argv": argv},
              "artifacts": {"stdout": ref + "#stdout", "stderr": ref + "#stderr"}}
    _record(rt, state, access, "command.run", result)
    return capture


def _citations(rt, state, citations):
    for cite in citations:
        ref, (start, end) = cite["ref"], cite["lines"]
        if ref not in state["allowed_refs"] or not ref.startswith("blob:"):
            raise InvestigationError("diagnosis cites evidence outside this task")
        blob = rt.store.get_blob(ref[5:])
        lines = blob.count(b"\n") + int(bool(blob) and not blob.endswith(b"\n"))
        if not 1 <= start <= end <= lines:
            raise InvestigationError("diagnosis citation is outside its source")


def _prepare_repair(rt, ws, spec, state, action):
    if len(state["repairs"]) >= spec["limits"]["max_repairs"]:
        raise ExecutionPaused("repair_budget")
    if state.get("analysis_fingerprint") != state["fingerprint"]:
        raise InvestigationError("analyze current evidence before proposing a repair")
    _citations(rt, state, action["support"] + action["counterevidence"])
    from ctx.anchors import parse_span
    for edit in action["edits"]:
        path = ws.relativize(ws.confine(edit["path"].removeprefix("repo:"), must_exist=True))
        if not within(path, spec["targets"]) or path in spec["witness_files"]:
            raise InvestigationError("edit is outside allowed targets or changes a verification witness")
        a, b, _ = parse_span(edit["span"])
        if not any(v.get("path") == path and v.get("snapshot") == edit["snapshot"] and
                   not v["transformed"] and v["start"] <= a <= b <= v["end"] for v in state["read_views"]):
            raise InvestigationError("read the exact, unredacted edit span before replacing it")
    plan = create_edit_plan(ws, rt.store, {"schema": "ctx.edit-request/v1", "edits": action["edits"]})
    preview = preview_edit_plan(ws, rt.store, plan)
    candidate = {"diagnosis": action["diagnosis"], "support": action["support"],
                 "counterevidence": action["counterevidence"], "plan": publish(rt.store, plan),
                 "preview": preview}
    state["repairs"].append(candidate)
    state["phase"] = "repair"
    _save(rt, state)  # full pre/post byte identities precede mutation


def _repair(rt, ws, lease, spec, state, access):
    index = len(state["repairs"]) - 1
    candidate = state["repairs"][index]
    plan = read(rt.store, candidate["plan"])
    preview = candidate["preview"]
    def reconcile(_):
        files = preview["files"]
        live = file_digests(ws, [f["path"] for f in files])
        before = {f["path"]: f["beforeSha256"] for f in files}
        after = {f["path"]: f["afterSha256"] for f in files}
        if live == after:
            return {**preview, "workspaceId": ws.workspace_id, "operation": "apply", "outcome": "applied",
                    "recovered": True}
        if live == before:
            return None
        raise ExecutionPaused("partially_applied_edit_requires_review")
    if "receipt" not in candidate:
        receipt = rt.perform(f"repair/{index}/apply", "edit.apply", {"plan": candidate["plan"]},
            lambda timeout: apply_edit_plan(ws, plan, attempt_key=rt.task_id + "/" + str(index)),
            kind="mutate", reconcile=reconcile)
        candidate["receipt"] = publish(rt.store, receipt)
        state["fingerprint"] = lease.fingerprint()
        _save(rt, state)
    _assert_current(ws, lease, spec, state)
    modified = set()
    for repair in state["repairs"]:
        modified.update(f["path"] for f in repair["preview"]["files"])
    n = 0
    def runner(check_ws, argv, *, timeout, store):
        nonlocal n
        key = f"repair/{index}/verify/{n}"
        n += 1
        return _capture(rt, check_ws, state, access, spec, lease, key, argv, timeout)
    proof = verify_edit(ws, rt.store, candidate["receipt"],
        [Check(c["kind"], tuple(c["argv"]), c["timeout"]) for c in spec["checks"]],
        witnesses=[*spec["witness_files"], *modified], runner=runner)
    candidate["verification"] = proof["verificationRef"]
    _record(rt, state, access, "edit.verify", {"rows": [proof], "meta": {"outcome": proof["outcome"]},
                                               "artifacts": {"verification": proof["verificationRef"]}})
    if proof["outcome"] == "passed":
        validate_verification(ws, rt.store, proof["verificationRef"])
        patch = lease.capture(spec["targets"])
        if not patch.data:
            raise ExecutionPaused("no_patch")
        if set(patch.changed_paths) != modified:
            raise ExecutionPaused("unreceipted_changes")
        state.update(status="verified", phase="complete", patch="blob:" + rt.store.put_blob(patch.data),
                     changed_paths=list(patch.changed_paths), verification=proof["verificationRef"])
    else:
        state.update(status="running", phase="investigate")
    state.pop("pending_request", None)
    state.pop("pending_decision", None)
    _save(rt, state)


def run(ws, store, task_id, *, worker=None, retry_failed=False):
    rt = TaskRuntime(ws, store, task_id, retry_failed=retry_failed)
    if (rt.binding.get("workspace") != str(ws.root.resolve()) or rt.binding.get("redaction") != policy_id(ws)
            or rt.binding.get("workspace_policy") != _workspace_policy(ws)
            or rt.binding.get("policy") != VERSION):
        raise InvestigationError("task workspace, policy, or redaction binding changed")
    spec = rt.checkpoint_value("investigation.spec")
    invoke = _worker(spec, worker, rt)
    with rt.active():
        state = rt.checkpoint_value("investigation")
        lease = PersistentWorktree(ws.root, store.root / "worktrees" / task_id, rt.binding["base"]).open()
        task_ws = replace(ws, root=lease.path)
        access = EvidenceAccess(tuple(spec["scopes"]), set(state["allowed_refs"]),
            spec["limits"]["source_bytes"], spec["limits"]["partition_bytes"], spec["limits"]["max_files"],
            spec["probes"], spec["worker"])
        pc = PlanContext(task_ws, store, runtime=rt, evidence=access, worker=invoke)
        try:
            if state["fingerprint"] is None:
                state["fingerprint"] = lease.fingerprint()
                _save(rt, state)
            # A prepared edit may have committed before its checkpoint. Only
            # the edit transaction's byte reconciliation may resolve that gap.
            if state["phase"] != "repair":
                _assert_current(task_ws, lease, spec, state)
            if state["phase"] == "complete":
                completion = state.get("completion", {"status": state["status"],
                                                       "stop_reason": state.get("stop_reason")})
                if completion["status"] == "verified":
                    validate_verification(task_ws, store, state["verification"])
                state.update(completion)
                _save(rt, state)
                return report(rt, state, lease)
            state["status"] = "running"
            state.pop("stop_reason", None)
            if state["phase"] == "baseline":
                for i in range(len(state["baseline"]), len(spec["checks"])):
                    check = spec["checks"][i]
                    captured = _capture(rt, task_ws, state, access, spec, lease,
                                        f"baseline/{i}", check["argv"], check["timeout"])
                    result = captured.manifest["result"]
                    if (result["timedOut"] or result.get("outputLimited") or result["signal"]
                            or result["exitCode"] not in [0, *check["failure_exit_codes"]]):
                        raise ExecutionPaused("baseline_check_invalid")
                    state["baseline"].append({"kind": check["kind"], "run": "run:" + captured.manifest_id,
                                               "failed": result["exitCode"] != 0})
                    _save(rt, state)
                if not any(c["kind"] == "behavior" and c["failed"] for c in state["baseline"]):
                    state.update(status="not_reproduced", phase="complete")
                    _save(rt, state)
                    return report(rt, state, lease)
                result = invoke_operation(pc, "evidence.discover", {}, key="initial-discovery")
                _record(rt, state, access, "evidence.discover", result)
                state["phase"] = "investigate"
                _save(rt, state)
            while state["phase"] != "complete":
                if state["phase"] == "repair":
                    _repair(rt, task_ws, lease, spec, state, access)
                    continue
                if state["round"] >= spec["limits"]["max_rounds"]:
                    raise ExecutionPaused("round_budget")
                _assert_current(task_ws, lease, spec, state)
                if "pending_decision" not in state:
                    if "pending_request" not in state:
                        state["pending_request"] = "blob:" + store.put_blob(_context(rt, spec, state))
                        _save(rt, state)
                    data = store.get_blob(state["pending_request"][5:])
                    bound = rt.model_worker(invoke, namespace=f"investigation/{state['round']}",
                                            validate=lambda raw: decision(parse_json(raw)))
                    outcome = bound(data, timeout=rt.limits.call_seconds, response_bytes=rt.limits.response_bytes)
                    _assert_current(task_ws, lease, spec, state)
                    if outcome.error or outcome.returncode not in (None, 0):
                        raise ExecutionPaused("decision_failed")
                    state["pending_decision"] = decision(parse_json(outcome.stdout))
                    _save(rt, state)
                action = state["pending_decision"]
                try:
                    if action["action"] == "stop":
                        state.update(status="inconclusive", phase="complete", stop_reason=action["reason"])
                    elif action["action"] == "repair":
                        _prepare_repair(rt, task_ws, spec, state, action)
                    else:
                        name, args = action["op"], dict(action["args"])
                        if name not in OPERATORS:
                            raise InvestigationError("operation is not enabled by this investigation policy")
                        if name in {"probe.run", "semantic.map"}:
                            args["sample"] = str(state["round"])
                        if name == "probe.run":
                            if state["probe_count"] >= spec["limits"]["max_probes"]:
                                raise ExecutionPaused("probe_budget")
                        result = invoke_operation(replace(pc, generation=state["fingerprint"]),
                                                  name, args, key=f"round/{state['round']}")
                        _assert_current(task_ws, lease, spec, state)
                        _record(rt, state, access, name, result)
                        if name == "probe.run":
                            state["probe_count"] += 1
                        if name == "semantic.map" and result["meta"]["status"] != "complete":
                            raise ExecutionPaused("semantic_analysis_incomplete")
                except (ValueError, KeyError, TypeError, OpError) as exc:
                    # Invalid actions become bounded observations; the host can
                    # correct them in a new, charged decision. No action executes
                    # after a failed contract check.
                    _record(rt, state, access, "action.refused", {"rows": [],
                        "meta": {"reason": type(exc).__name__, "detail": str(exc)[:300]}, "artifacts": {}})
                state["round"] += 1
                state.pop("pending_request", None)
                state.pop("pending_decision", None)
                _save(rt, state)
        except ExecutionPaused as exc:
            state.update(status="paused", stop_reason=str(exc))
            _save(rt, state)
        except BaseException:
            state.update(status="interrupted", stop_reason="interrupted")
            _save(rt, state)
            raise
        return report(rt, state, lease)


def report(rt, state=None, lease=None):
    state = state or rt.checkpoint_value("investigation")
    operations = list(rt.state().operations.values())
    value = {"schema": "ctx.investigation-report/v1", "task_id": rt.task_id,
        "status": state["status"], "phase": state["phase"], "stop_reason": state.get("stop_reason"),
        "base": rt.binding["base"], "totals": rt.totals(), "limits": rt.limits.as_dict(),
        "rounds": state["round"], "repairs": state["repairs"], "baseline": state["baseline"],
        "patch": state.get("patch"), "verification": state.get("verification"),
        "observations": state["observations"], "operations": operations,
        "coverage": {"selection_complete": None, "verification_scope": "caller-selected checks"},
        "worktree": str(lease.path) if lease else str(rt.store.root / "worktrees" / rt.task_id),
        "state": rt.state().checkpoints.get("investigation"), "execution": rt.retain()}
    ref = publish(rt.store, value)
    return ref, value


def inspect(ws, store, task_id):
    return report(TaskRuntime(ws, store, task_id))


def apply(ws, store, task_id):
    """Explicit handoff into a still-clean original checkout at the pinned base."""
    rt = TaskRuntime(ws, store, task_id)
    with rt.active():
        state = rt.checkpoint_value("investigation")
        if state["status"] != "verified" or ws.git.head != rt.binding["base"] or not clean_git_root(ws.root):
            raise InvestigationError("apply needs a verified task and its clean original base checkout")
        lease = PersistentWorktree(ws.root, store.root / "worktrees" / task_id, rt.binding["base"]).open()
        _assert_current(replace(ws, root=lease.path), lease,
                        rt.checkpoint_value("investigation.spec"), state)
        validate_verification(replace(ws, root=lease.path), store, state["verification"])
        patch = WorktreePatch(store.get_blob(state["patch"][5:]), tuple(state["changed_paths"]))
        def commit(timeout):
            ok, _ = preflight_patch(ws.root, patch)
            if not ok:
                raise InvestigationError("patch no longer applies to the original checkout")
            ok, _ = apply_patches(ws.root, [patch])
            if not ok:
                raise InvestigationError("patch handoff failed")
            return {"patch": state["patch"], "outcome": "applied"}
        return rt.perform("handoff", "patch.apply", {"patch": state["patch"]}, commit, kind="mutate")
