"""Evidence-plan executor + the ``investigate/v1`` digest (docs/EVIDENCE-PLANS.md).

Execution model — deterministic by construction:

- Steps run in plan order (validation guarantees edges point backward, so
  plan order IS a topological order). Result bytes are independent of any
  scheduling choice; parallel waves are a latency optimization the byte
  contract permits later, never a semantics change.
- Every node result is persisted as a content-addressed canonical-JSON
  blob (``ctx.plan-node/v1``) — per-node provenance, addressable via
  ``ctx get blob:<id> --json-pointer /rows``. Timing never enters the
  payload (volatile quarantine); durations live in the operational trace.
- Observe-class node results are cached keyed by (op, canonical args,
  input blob id, source-state generation, engine identity): a replan
  re-executes only the frontier that changed — the one-replan epoch
  allowance is cheap because the unchanged prefix is free.
- ``when`` guards, ``on_missing`` engine absences, ``on_error``
  skip-dependents cascades, wall-budget exhaustion: every non-executed
  node is DECLARED in the coverage section with its typed reason. The
  digest always renders.

The digest is a materialized answer, organized causally (never by command
order): ranked conclusion candidates with plane attribution, a REQUIRED
counterevidence section (present even when empty — the anti-anchoring
guard), the coverage attestation, and retrieval addresses for every node.
It renders through the shipped EDC resolver against
``contracts/investigate.toml`` — no new policy machinery.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ctx import bounds
from ctx.gitstatus import changed_paths
from ctx.sessiondir import LEDGER_DIR_NAME, session_reads_path
from ctx.store import Store, canonical_json
from ctx.workspace import Workspace, stat_fingerprint

NODE_SCHEMA = "ctx.plan-node/v1"
INVESTIGATION_SCHEMA = "ctx.investigation/v1"
PROFILE_VERSION = "investigate/v1"

#: Typed non-execution reasons (closed, ledger-shaped).
SKIP_REASONS = (
    "guard_not_met",
    "guard_undecidable",
    "upstream_failed",
    "upstream_skipped",
    "engine_missing",
    "budget_wall_exhausted",
    "plan_halted",
)

_CANDIDATE_CAP = 8
_TESTS_PER_CANDIDATE = 4
_COUNTER_CAP = 6
_NEXT_CAP = 3


class PlanExecutionError(Exception):
    pass


# ------------------------------------------------------------- node cache
def _ensure_cache_table(store: Store) -> None:
    with store.db:
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS plan_cache ("
            " key TEXT PRIMARY KEY, blob TEXT NOT NULL, created_at REAL NOT NULL)"
        )


def _workspace_fingerprint(ws: Workspace) -> str | None:
    """Content-sensitive workspace identity for node-cache keys.

    The facts *generation* (porcelain + untracked triples) is operational
    identity and deliberately blind to content edits of already-modified
    TRACKED files — correct for rerun classification, too weak for a
    result cache. This fingerprint adds HEAD plus the shared stat basis
    (:func:`ctx.workspace.stat_fingerprint`: size, mtime_ns, ctime_ns) for
    every porcelain-listed path, so any edit that touches a listed file
    invalidates — including one that restores the old mtime. None (non-git
    / git error) disables caching — an unknown state must never serve a
    cached result."""
    import subprocess

    if ws.git is None:
        return None
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ws.root),
            capture_output=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        h = hashlib.sha256((ws.git.head or "").encode("utf-8"))
        h.update(out.stdout)
        from pathlib import Path

        root = Path(ws.root)
        listed: list[Path] = []
        for rel in changed_paths(out.stdout, exclude_top=LEDGER_DIR_NAME):
            p = root / rel
            if rel.endswith("/") or p.is_dir():
                listed.extend(s for s in sorted(p.rglob("*"))[:1024] if s.is_file())
            elif p.is_file():
                listed.append(p)
        stat_fingerprint(root, sorted(listed)[:2048], h)
        return h.hexdigest()
    except Exception:
        return None


def _node_cache_key(op: str, args: dict, input_blob: str | None,
                    fingerprint: str, engine: str) -> str:
    """Key for one executed plan node. Invalidation basis: the node itself
    (op/args/input) plus ``_workspace_fingerprint`` — HEAD, raw porcelain
    bytes, and the shared stat basis over every listed path."""
    seed = canonical_json(
        {"op": op, "args": args, "input": input_blob, "ws": fingerprint, "engine": engine}
    )
    return hashlib.sha256(seed).hexdigest()


def _cache_get(store: Store, key: str) -> str | None:
    try:
        row = store.db.execute(
            "SELECT blob FROM plan_cache WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _cache_put(store: Store, key: str, blob_id: str) -> None:
    try:
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO plan_cache (key, blob, created_at) VALUES (?,?,?)",
                (key, blob_id, time.time()),
            )
    except Exception:
        pass


def _engine_identity(spec: Any) -> str:
    """Engine identity for cache keys: which physical engine would serve
    this op right now (version included where the engine discloses one)."""
    name = spec.name
    try:
        if name.startswith("ast."):
            from ctx import astgrep

            return astgrep.engine_id()
        if name.startswith("semantic."):
            from ctx import semgrep_engine

            return semgrep_engine.engine_id()
        if name == "code.refs":
            from ctx.codeverbs import _select_engine

            return _select_engine()
    except Exception:
        pass
    return "builtin"


# -------------------------------------------------------------- guard eval
def _eval_when(when: str, results: dict[str, dict]) -> tuple[bool | None, str]:
    """Evaluate a validated guard against upstream results. Returns
    (verdict, note); verdict None = undecidable (e.g. outcome guard on a
    node without an outcome) — the node is skipped, declared."""
    from ctx.plan_ir import _WHEN_COUNT_RE, _WHEN_OUTCOME_RE

    m = _WHEN_COUNT_RE.match(when)
    if m:
        node, op, num = m.group(1), m.group(2), int(m.group(3))
        res = results.get(node)
        if res is None or res.get("status") != "ok":
            return None, f"guard input {node!r} did not produce a result"
        count = len(res.get("rows") or [])
        verdict = {
            "==": count == num, "!=": count != num, ">": count > num,
            ">=": count >= num, "<": count < num, "<=": count <= num,
        }[op]
        return verdict, f"{node}.count={count}"
    m = _WHEN_OUTCOME_RE.match(when)
    if m:
        node, op, want = m.group(1), m.group(2), m.group(3)
        res = results.get(node)
        outcome = (res or {}).get("meta", {}).get("outcome")
        if outcome not in ("pass", "fail"):
            return None, f"guard input {node!r} carries no outcome"
        verdict = (outcome == want) if op == "==" else (outcome != want)
        return verdict, f"{node}.outcome={outcome}"
    return None, "guard did not match the validated grammar"  # unreachable post-validation


def _foreach_expand(step: Any, inp: dict, max_fanout: int) -> tuple[list[Any], int]:
    """Values of the foreach field from the input rows, deduped in row
    order, capped (cap validated ≤ max_fanout). Returns (values, omitted)."""
    values: list[Any] = []
    for row in inp.get("rows") or []:
        v = row.get(step.foreach)
        if v is not None and v not in values:
            values.append(v)
    # bounds, not `or`: a plan declaring `cap: 0` for a foreach step means
    # "fan out over nothing", and `or` read that as "unset" and substituted
    # max_fanout -- the widest possible fan-out from the narrowest possible
    # request. A negative cap reached values[:cap] as a suffix slice too.
    cap = min(bounds.count(bounds.explicit(step.cap, max_fanout)), max_fanout)
    return values[:cap], max(0, len(values) - cap)


def _subst_item(args: dict, item: Any) -> dict:
    """Merge one foreach item into args: ``{item}`` placeholders in string
    values are substituted; the raw value also rides as ``args["item"]``."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        out[k] = v.replace("{item}", str(item)) if isinstance(v, str) else v
    out["item"] = item
    return out


# --------------------------------------------------------------- execution
def execute_plan(
    ws: Workspace,
    store: Store,
    plan_text_or_doc: str | dict,
    *,
    tier: str = "cli",
    clock=time.monotonic,
    node_rows: dict[str, int] | None = None,
) -> tuple[str, int]:
    """Validate and execute one evidence plan; returns (digest, exit_code).

    Exit codes: 0 = executed (test failures are evidence, not errors);
    2 = validation rejections (the text IS the typed rejection list);
    3 = one or more nodes errored (digest still renders, errors declared).

    ``node_rows``, when supplied, is filled in place with the realized row
    count of every ok node (additive out-parameter — the text/exit
    contract is unchanged). Skipped and errored nodes never appear:
    absence means the node produced nothing.
    """
    from ctx import plan_ir, plan_ops

    try:
        plan = plan_ir.parse_plan(plan_text_or_doc)
    except plan_ir.PlanError as e:
        return f"ctx plan: {e}", 2

    rejections = plan_ir.validate_plan(plan, tier=tier, plan_policy=ws.config.plan)
    if rejections:
        lines = [f"[ctx plan · REJECTED · {len(rejections)} problem(s)]"]
        lines += ["  " + r.render() for r in rejections]
        return "\n".join(lines), 2

    _ensure_cache_table(store)
    plan_blob = store.put_blob(plan.canonical_bytes())
    from ctx import facts

    # The emissions ledger FILE must exist BEFORE the generation is read:
    # the generation hashes raw porcelain bytes, and porcelain gains a
    # ``?? .ctx-session-reads/`` line only once a file exists inside the
    # (excluded-from-triples, but not from raw bytes) dir. Creating it
    # mid-run would de-sync the recorded generation from any post-run read
    # and break replan byte-determinism. Fail-open: an unwritable root just
    # skips emissions; in ignored/committed setups this is a no-op.
    try:
        ledger_dir = session_reads_path(ws.root)
        ledger_dir.mkdir(parents=True, exist_ok=True)
        (ledger_dir / "plan-emissions.jsonl").touch(exist_ok=True)
    except OSError:
        pass

    generation = facts.current_generation(ws)
    ws_fingerprint = _workspace_fingerprint(ws)
    pc = plan_ops.PlanContext(ws=ws, store=store, generation=generation)

    _wall_seconds = plan.budget.get("wall_seconds", ws.config.plan.wall_seconds)  # fix: an explicit null must fall back too, not crash float(None)
    wall_budget = float(_wall_seconds if _wall_seconds is not None else ws.config.plan.wall_seconds)
    max_fanout = min(
        int(plan.budget.get("max_fanout", ws.config.plan.max_fanout)),
        ws.config.plan.max_fanout,
        plan_ir.MAX_FANOUT_HARD,
    )
    started = clock()

    results: dict[str, dict[str, Any]] = {}  # node id -> executed payload + status
    node_meta: dict[str, dict[str, Any]] = {}  # node id -> blob/status/reason/duration
    dead: set[str] = set()  # ids whose dependents must be skipped
    halted = False

    for step in plan.steps:
        spec = plan_ops.OPS[step.op]
        entry: dict[str, Any] = {"op": step.op, "status": "ok", "reason": None}
        node_meta[step.id] = entry

        if halted:
            entry.update(status="skipped", reason="plan_halted")
            dead.add(step.id)
            continue
        upstream_dead = [u for u in step.upstream() if u in dead]
        if upstream_dead:
            up = upstream_dead[0]
            reason = (
                "upstream_failed"
                if node_meta.get(up, {}).get("status") == "error"
                else "upstream_skipped"
            )
            entry.update(status="skipped", reason=reason, detail=up)
            dead.add(step.id)
            continue
        if clock() - started > wall_budget:
            entry.update(status="skipped", reason="budget_wall_exhausted")
            dead.add(step.id)
            continue
        if step.when is not None:
            verdict, note = _eval_when(step.when, results)
            if verdict is None:
                entry.update(status="skipped", reason="guard_undecidable", detail=note)
                dead.add(step.id)
                continue
            if not verdict:
                entry.update(status="skipped", reason="guard_not_met", detail=note)
                dead.add(step.id)
                continue

        effective_missing = step.on_missing or spec.on_missing_default
        if spec.probe_available is not None and not spec.probe_available():
            if effective_missing == "skip":
                entry.update(
                    status="skipped", reason="engine_missing",
                    detail=spec.engine_hint or spec.name,
                )
                dead.add(step.id)
                continue
            if effective_missing == "fail":
                # Validation rejects this combination statically; reaching it
                # means the engine vanished between validate and execute.
                entry.update(status="error", reason="engine_missing")
                dead.add(step.id)
                if step.on_error == "fail":
                    halted = True
                continue
            # degrade: the op's own fallback chain handles it.

        inp = results.get(step.input) if step.input else None
        input_blob = node_meta.get(step.input, {}).get("blob") if step.input else None
        engine = _engine_identity(spec)

        cache_key = None
        if spec.cacheable and spec.klass == "observe" and ws_fingerprint is not None:
            cache_key = _node_cache_key(step.op, dict(step.args), input_blob, ws_fingerprint, engine)
            hit = _cache_get(store, cache_key)
            if hit is not None:
                try:
                    import json as _json

                    doc = _json.loads(store.get_blob(hit).decode("utf-8"))
                    results[step.id] = {**doc, "status": "ok"}
                    entry.update(blob=hit, cached=True)
                    continue
                except Exception:
                    pass  # fall through to live execution

        t0 = clock()
        try:
            if step.foreach is not None and inp is not None:
                values, fanout_omitted = _foreach_expand(step, inp, max_fanout)
                rows: list[dict[str, Any]] = []
                omitted = fanout_omitted
                metas: list[dict[str, Any]] = []
                artifacts: dict[str, str] = {}
                kind = spec.output_kind
                for item in values:
                    part = spec.fn(pc, _subst_item(step.args, item), inp)
                    rows.extend(part.get("rows") or [])
                    omitted += int(part.get("omitted") or 0)
                    metas.append(part.get("meta") or {})
                    artifacts.update(part.get("artifacts") or {})
                    kind = part.get("kind") or kind
                engines = sorted({str(m.get("engine")) for m in metas if m.get("engine")})
                out = plan_ops.payload(
                    kind, rows, omitted=omitted,
                    meta={"engine": "+".join(engines) or "none", "foreach": len(values)},
                    artifacts=artifacts,
                )
            else:
                out = spec.fn(pc, dict(step.args), inp)
            if len(out["rows"]) > spec.row_cap:
                out["omitted"] += len(out["rows"]) - spec.row_cap
                out["rows"] = out["rows"][: spec.row_cap]
        except Exception as e:
            entry.update(status="error", reason=type(e).__name__, detail=str(e)[:200])
            dead.add(step.id)
            if step.on_error == "fail":
                halted = True
            continue
        finally:
            entry["duration_s"] = round(clock() - t0, 3)  # operational trace only

        doc = {
            "format": NODE_SCHEMA,
            "plan": plan_blob[:12],
            "node": step.id,
            "op": step.op,
            **{k: out[k] for k in ("kind", "rows", "omitted", "meta", "artifacts")},
        }
        blob_id = store.put_blob(canonical_json(doc))
        if cache_key is not None:
            _cache_put(store, cache_key, blob_id)
        results[step.id] = {**doc, "status": "ok"}
        entry.update(blob=blob_id)

    if node_rows is not None:
        # Realized rows per ok node: declared OpSpec.provides are estimates;
        # advisory coverage must be conditioned on what actually landed.
        node_rows.update({nid: len(res.get("rows") or []) for nid, res in results.items()})

    # ---------------------------------------------------------- investigation
    manifest = {
        "schema": INVESTIGATION_SCHEMA,
        "workspaceId": ws.workspace_id,
        "plan": f"sha256:{plan_blob}",
        "objective": plan.question,
        "generation": generation,
        "tier": tier,
        "nodes": {
            nid: {
                k: v
                for k, v in meta.items()
                if k in ("op", "status", "reason", "detail", "blob", "cached")
            }
            for nid, meta in node_meta.items()
        },
        "digest": {"profile": PROFILE_VERSION, "policy": "default/v1"},
    }
    inv_id = store.put_manifest(manifest, kind="investigation")

    # Live plan emissions (ctx.plan-emission/v1): one JSONL line per executed
    # ok node into the house ledger dir, feeding evidence-outcome attribution
    # with REAL generations and per-node cost (debts 936231223f / 741c6afb40).
    # Fail-open like the telemetry block below: any error here must never
    # affect the digest bytes or the exit code. "ts" is operational-only,
    # exactly like the investigations ledger.
    try:
        _append_plan_emissions(ws, inv_id, generation, results, node_meta)
    except Exception:
        pass

    text = _render_investigation(
        ws, store, plan, plan_blob, inv_id, generation, results, node_meta
    )

    try:
        from ctx.retrieval import record_telemetry

        raw = sum(
            len(canonical_json(results[nid])) for nid in results
        )
        record_telemetry(store, "plan", raw, len(text.encode("utf-8")))
    except Exception:
        pass

    errored = any(m.get("status") == "error" for m in node_meta.values())
    return text, (3 if errored else 0)


_EMISSION_SCHEMA = "ctx.plan-emission/v1"
_EMISSION_LIST_CAP = 16


def _emission_identities(rows: Any, key: str) -> list[str]:
    """Sorted unique non-empty values of one optional row field, defensively
    extracted (rows are dicts with optional keys), capped at the emission
    list cap. Truncation is undeclared at this layer by design — the
    attribution join treats identity sets as samples, never censuses."""
    out: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        v = row.get(key)
        if v:
            out.add(str(v))
    return sorted(out)[:_EMISSION_LIST_CAP]


def _append_plan_emissions(
    ws: Workspace,
    inv_id: str,
    generation: str | None,
    results: dict[str, dict[str, Any]],
    node_meta: dict[str, dict[str, Any]],
) -> None:
    """Append one ``ctx.plan-emission/v1`` line per executed ok node to the
    house ledger (``.ctx-session-reads/plan-emissions.jsonl``). Carries the
    REAL source-state generation and per-node cost (duration, visible-token
    estimate) so evidence-outcome attribution stops approximating either.
    Callers wrap this fail-open; it never raises into the digest path."""
    import json as _json

    from ctx.textutil import estimate_tokens

    ledger_dir = session_reads_path(ws.root)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for nid, res in results.items():
        meta = node_meta.get(nid) or {}
        if meta.get("status") != "ok":
            continue
        rows = res.get("rows") or []
        artifacts = res.get("artifacts") or {}
        handles = sorted(
            str(v)
            for v in (artifacts.values() if isinstance(artifacts, dict) else ())
            if isinstance(v, str) and v.startswith(("run:", "blob:"))
        )[:_EMISSION_LIST_CAP]
        try:
            duration_ms = int(float(meta.get("duration_s") or 0.0) * 1000)
        except (TypeError, ValueError):
            duration_ms = 0
        rec = {
            "schema": _EMISSION_SCHEMA,
            "investigation_id": inv_id,
            "plan_node_id": nid,
            "op": str(res.get("op") or meta.get("op") or ""),
            "generation": generation,
            "files": _emission_identities(rows, "file"),
            "symbols": _emission_identities(rows, "symbol"),
            "tests": _emission_identities(rows, "test"),
            "handles": handles,
            "rows": len(rows),
            "duration_ms": duration_ms,
            "visible_tokens": estimate_tokens(len(canonical_json(res))),
            "ts": time.time(),  # operational-only, like the investigations ledger
        }
        lines.append(_json.dumps(rec, sort_keys=True))
    if lines:
        with (ledger_dir / "plan-emissions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------- rendering
def _build_candidates(
    results: dict[str, dict], node_meta: dict[str, dict]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Conclusion candidates from failing_in_changed join nodes, aggregated
    per (symbol, file): the changed symbol that explains failing tests.
    Returns (candidates, contributing node ids)."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    sources: list[str] = []
    static_files: set[str] = set()
    semantic_files: set[str] = set()
    for nid, res in results.items():
        meta = res.get("meta") or {}
        op = str(res.get("op") or "")
        if op == "ast.search":
            static_files |= {str(r.get("file") or "") for r in res.get("rows") or []}
        if op.startswith("semantic."):
            semantic_files |= {str(r.get("file") or "") for r in res.get("rows") or []}
        if meta.get("join") != "failing_in_changed":
            continue
        sources.append(nid)
        for row in res.get("rows") or []:
            symbol = row.get("symbol") or "(file-level)"
            key = (str(symbol), str(row.get("file") or ""))
            b = buckets.setdefault(
                key,
                {
                    "symbol": key[0],
                    "file": key[1],
                    "line": int(row.get("line") or 0),
                    "tests": [],
                    "failure_classes": [],
                    "span": row.get("span"),
                    "precision": row.get("precision"),
                    "node": nid,
                },
            )
            t = str(row.get("test") or "")
            if t and t not in b["tests"]:
                b["tests"].append(t)
            fc = row.get("failure_class")
            if fc and fc not in b["failure_classes"]:
                b["failure_classes"].append(fc)
            b["line"] = min(b["line"] or int(row.get("line") or 0), int(row.get("line") or 0))
    candidates = list(buckets.values())
    for c in candidates:
        planes = ["dynamic", "temporal"]  # by construction of the join
        if c["symbol"] != "(file-level)":
            planes.append("static")
        if c["file"] in semantic_files:
            planes.append("semantic")
        elif c["file"] in static_files:
            pass  # structural sighting alone doesn't add a plane beyond static
        c["planes"] = planes
    return candidates, sources


_RANK_FNS = {
    # Lower sorts first; every key deterministic from extracted facts.
    "dynamic_confirmation": lambda c: -len(c["tests"]),
    "changedness": lambda c: 0,  # all candidates are changed by construction
    "causal_proximity": lambda c: 0 if c["symbol"] != "(file-level)" else 1,
    "semantic_confidence": lambda c: 0 if "semantic" in c["planes"] else 1,
}


def _rank_candidates(candidates: list[dict], rank_by: tuple[str, ...]) -> list[dict]:
    keys = [k for k in rank_by if k in _RANK_FNS] or ["dynamic_confirmation"]

    def sort_key(c: dict):
        return tuple(_RANK_FNS[k](c) for k in keys) + (c["file"], c["line"], c["symbol"])

    return sorted(candidates, key=sort_key)


def _build_graph(plan: Any, candidates: list[dict], generation: str | None):
    """The typed investigation graph: candidates as items, relations wired
    from the join facts (graph v2 — the first consumer of relations)."""
    from ctx.evidence import EvidenceGraph, EvidenceItem, EvidenceRef

    items = []
    relations: list[tuple[str, str, str]] = []
    for i, c in enumerate(candidates):
        cid = f"{c['file']}::{c['symbol']}"
        ref = None
        if c.get("span"):
            # Minted region span over the source snapshot (decl fact):
            # resolvable via `ctx get repo:<file> --span <id>`.
            ref = EvidenceRef(artifact=f"repo:{c['file']}", selector=f"span:{c['span']}")
        items.append(
            EvidenceItem(
                id=cid,
                kind="conclusion_candidate",
                severity="error",
                summary=(
                    f"{len(c['tests'])} failing test(s) locate in changed symbol "
                    f"{c['symbol']}"
                ),
                failure_class=(c["failure_classes"][0] if c["failure_classes"] else None),
                location=f"{c['file']}:{c['line']}",
                detail_ref=ref,
                causal_rank=i,
            )
        )
        for t in c["tests"]:
            relations.append((t, "frame_of", cid))
        if generation:
            relations.append((cid, "changed_in", f"gen:{generation}"))
    outcome = "fail" if items else "pass"
    return EvidenceGraph(
        family="investigate",
        profile_version=PROFILE_VERSION,
        outcome=outcome,
        aggregate={"candidates": len(items)},
        items=tuple(items),
        artifacts={},
        coverage={"parsed": len(items), "total_estimate": len(items), "complete": True},
        relations=tuple(relations),
    )


def _render_investigation(
    ws: Workspace,
    store: Store,
    plan: Any,
    plan_blob: str,
    inv_id: str,
    generation: str | None,
    results: dict[str, dict],
    node_meta: dict[str, dict],
) -> str:
    from ctx.contracts import contract_for_family, validate_selection
    from ctx.textutil import fmt_int

    candidates, _sources = _build_candidates(results, node_meta)
    ranked = _rank_candidates(candidates, plan.rank_by)
    graph = _build_graph(plan, ranked, generation)

    counter_rows: list[dict[str, Any]] = []
    counter_nodes: list[str] = []
    for nid, res in results.items():
        if (res.get("meta") or {}).get("join") == "untouched_failures":
            counter_nodes.append(nid)
            counter_rows.extend(res.get("rows") or [])

    n_ok = sum(1 for m in node_meta.values() if m.get("status") == "ok")
    n_skip = sum(1 for m in node_meta.values() if m.get("status") == "skipped")
    n_err = sum(1 for m in node_meta.values() if m.get("status") == "error")

    lines = [f"[ctx plan run:{inv_id[:12]} profile={PROFILE_VERSION}]"]
    lines.append(f"objective: {plan.question}")
    lines.append(
        f"plan: blob:{plan_blob[:12]} · generation: {generation or 'unknown'} · "
        f"nodes: {n_ok} ok · {n_skip} skipped · {n_err} failed"
    )

    sections = plan.sections or ("conclusion_candidates", "counterevidence", "coverage")

    if "conclusion_candidates" in sections:
        lines.append(f"conclusion candidates (census): {fmt_int(len(ranked))}")
        for i, c in enumerate(ranked[:_CANDIDATE_CAP], start=1):
            classes = "+".join(c["failure_classes"][:2]) or "?"
            planes = "+".join(c["planes"])
            lines.append(
                f"  {i}. {c['symbol']} · repo:{c['file']}:L{c['line']} · "
                f"{fmt_int(len(c['tests']))} test(s) · {classes} · planes {planes}"
            )
            shown_tests = c["tests"][:_TESTS_PER_CANDIDATE]
            tail = (
                f" · +{fmt_int(len(c['tests']) - len(shown_tests))} more"
                if len(c["tests"]) > len(shown_tests)
                else ""
            )
            lines.append(f"     tests: {', '.join(shown_tests)}{tail}")
            if c.get("precision"):
                lines.append(f"     precision: {c['precision']}")
        if len(ranked) > _CANDIDATE_CAP:
            lines.append(f"  … +{fmt_int(len(ranked) - _CANDIDATE_CAP)} more candidates (addressed below)")
        if not ranked:
            lines.append(
                "  none — no failing test locates in a changed symbol this generation"
            )

    if "counterevidence" in sections:
        lines.append("counterevidence:")
        if counter_rows:
            for r in counter_rows[:_COUNTER_CAP]:
                lines.append(
                    f"  - {r.get('test')} · repo:{r.get('file')}:L{r.get('line')} · "
                    "failure in unchanged code"
                )
            if len(counter_rows) > _COUNTER_CAP:
                lines.append(f"  … +{fmt_int(len(counter_rows) - _COUNTER_CAP)} more")
        else:
            probes = len(counter_nodes) if counter_nodes else n_ok
            lines.append(f"  none found ({fmt_int(probes)} probe(s) executed)")

    if "coverage" in sections:
        lines.append("coverage:")
        for step in plan.steps:
            meta = node_meta.get(step.id) or {}
            status = meta.get("status")
            if status == "ok":
                res = results.get(step.id) or {}
                engine = (res.get("meta") or {}).get("engine") or "?"
                n = len(res.get("rows") or [])
                om = int(res.get("omitted") or 0)
                extra = f" · omitted {fmt_int(om)}" if om else ""
                run_ref = (res.get("artifacts") or {}).get("run")
                run_s = f" · {run_ref}" if run_ref else ""
                outcome = (res.get("meta") or {}).get("outcome")
                out_s = f" · outcome {outcome}" if outcome else ""
                prec = (res.get("meta") or {}).get("precision")
                prec_s = f" · {prec}" if prec and prec != "structural" else ""
                lines.append(
                    f"  {step.id} · {step.op} · engine {engine} · "
                    f"{fmt_int(n)} rows{extra}{run_s}{out_s}{prec_s}"
                )
            elif status == "skipped":
                why = meta.get("reason")
                detail = meta.get("detail")
                d = f" ({detail})" if detail else ""
                lines.append(f"  {step.id} · {step.op} · SKIPPED: {why}{d}")
            else:
                lines.append(
                    f"  {step.id} · {step.op} · ERROR: {meta.get('reason')} "
                    f"({meta.get('detail') or '?'})"
                )

    # Retrieval addresses: the top-ranked evidence first, then the manifest.
    next_lines: list[str] = []
    for c in ranked[:_NEXT_CAP]:
        blob = node_meta.get(c["node"], {}).get("blob")
        if blob:
            next_lines.append(f"ctx get blob:{blob[:12]} --json-pointer /rows")
            break
    for nid in list(counter_nodes)[:1]:
        blob = node_meta.get(nid, {}).get("blob")
        if blob:
            next_lines.append(f"ctx get blob:{blob[:12]} --json-pointer /rows")
    for step in plan.steps:
        if len(next_lines) >= _NEXT_CAP:
            break
        blob = node_meta.get(step.id, {}).get("blob")
        res = results.get(step.id) or {}
        if blob and (res.get("artifacts") or {}).get("run"):
            next_lines.append(f"ctx get {res['artifacts']['run']}#stdout --lines 1:40")
    if next_lines:
        lines.append("next:")
        lines.extend(f"  {n}" for n in dict.fromkeys(next_lines))

    # Contract check at the selection seam: the receipt is computed over
    # typed facts; a violated required class is a bug, surfaced loudly.
    try:
        contract = contract_for_family("investigate")
        # Declare what this run actually delivered, not what the shape of an
        # investigation usually delivers. `counterevidence` was in this set
        # unconditionally, and the validator's catch-all then took the
        # declaration as proof -- so a contract that marks the class REQUIRED
        # with loss_severities = "major", precisely because "its absence is
        # exactly the anchoring failure the plan exists to prevent", was
        # satisfied by the word appearing in a literal.
        included = {
            "aggregate_counts",
            "complete_identity_census",
            "location",
            "one_line_summary",
            "coverage_attestation",
        }
        witnessed: set[str] = set()
        if counter_nodes:
            # This run actually produced counterevidence, and this is the
            # only layer that can know it -- so this is the only layer that
            # may attest it.
            included.add("counterevidence")
            witnessed.add("counterevidence")
        receipt = validate_selection(
            (i.id for i in graph.items), included, contract, graph, witnessed
        )
        if receipt.required_fraction < 1.0:
            note = ""
            if receipt.unverifiable_fields:
                # Name them: "PARTIAL 5/6" with no reason is an alarm nobody
                # can act on, and an unverifiable requirement is a fact about
                # the CONTRACT, not about this run.
                note = (
                    " · unverifiable: " + ", ".join(receipt.unverifiable_fields)
                )
            lines.append(
                f"contract: PARTIAL — required classes {receipt.required_fields_present}"
                f"/{receipt.required_fields_total} (declared, never silent){note}"
            )
    except Exception:
        pass

    return "\n".join(lines)


__all__ = [
    "NODE_SCHEMA",
    "INVESTIGATION_SCHEMA",
    "PROFILE_VERSION",
    "SKIP_REASONS",
    "PlanExecutionError",
    "execute_plan",
]
