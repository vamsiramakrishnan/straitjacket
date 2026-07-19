"""Logical evidence-plan operators (docs/EVIDENCE-PLANS.md).

Each op is registered like a ``ctx q`` stage, generalized with a capability
class, a cost class, and engine disclosure — a strict superset of
``query.register_stage``. The model specifies epistemic intent (``code.refs``,
``ast.search``, ``evidence.join``); physical engines are the harness's
choice, deterministic given availability, and disclosed per node — fallbacks
are never anonymous (CONTRIBUTING rule).

Ops wrap SHIPPED machinery wherever it exists: the q stage registry
(refs/callers/callees/impact/search + combinators), the facts store's
Angle-lite joins, the skeleton tier, the birth-gate capture, and the
ast-grep / Semgrep engine modules. An op function returns a plain payload::

    {"kind": <stream kind>, "rows": [...], "omitted": int,
     "meta": {...engine, precision, notes}, "artifacts": {...}}

Timing never enters the payload (volatile quarantine); the executor
persists each payload as a content-addressed canonical-JSON blob.

Capability classes (SPEC §10.4): ``observe`` ops are bounded-only and
MCP-eligible; ``execute`` ops (test.run, ast.rewrite.apply) run commands
or mutate the worktree and stay on the CLI where the host's permission
flow is visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ctx.store import Store
from ctx.workspace import Workspace

DEFAULT_ROW_CAP = 200
OUTLINE_FILE_CAP = 12


class OpError(Exception):
    pass


@dataclass(frozen=True)
class OpSpec:
    """One registered logical operator (the q Stage contract, extended)."""

    name: str
    fn: Callable  # fn(pc, args, input_payload|None) -> payload dict
    input_kinds: tuple  # () = source op
    output_kind: str  # sites | files | records | text
    klass: str = "observe"  # observe | execute
    cost: str = "scan"  # index | scan | process | test (plan_ir.COST_UNITS)
    doc: str = ""
    row_cap: int = DEFAULT_ROW_CAP
    on_missing_default: str = "degrade"  # degrade | skip | fail
    probe_available: Callable[[], bool] | None = None  # None = always available
    engine_hint: str | None = None
    check_args: Callable[[dict], str | None] | None = None
    # Node-cache eligibility. Deliberately conservative: only expensive
    # external-engine scans opt in — the shipped verbs already carry their
    # own content-keyed caches at the correct layer (skeletons by source
    # blob hash, callgraph by worktree mtime, repomap by worktree content),
    # and double-caching is where staleness bugs live.
    cacheable: bool = False
    input_optional: bool = False  # may run without an input (narrows with one)


OPS: dict[str, OpSpec] = {}


def register_op(
    name: str,
    fn: Callable,
    *,
    input_kinds: tuple,
    output_kind: str,
    klass: str = "observe",
    cost: str = "scan",
    doc: str = "",
    row_cap: int = DEFAULT_ROW_CAP,
    on_missing_default: str = "degrade",
    probe_available: Callable[[], bool] | None = None,
    engine_hint: str | None = None,
    check_args: Callable[[dict], str | None] | None = None,
    cacheable: bool = False,
    input_optional: bool = False,
) -> None:
    OPS[name] = OpSpec(
        name,
        fn,
        tuple(input_kinds),
        output_kind,
        klass,
        cost,
        doc,
        row_cap,
        on_missing_default,
        probe_available,
        engine_hint,
        check_args,
        cacheable,
        input_optional,
    )


@dataclass
class PlanContext:
    ws: Workspace
    store: Store
    timeout: float = 600.0
    generation: str | None = None
    trace: list[str] = field(default_factory=list)


def payload(
    kind: str,
    rows: list[dict[str, Any]],
    *,
    omitted: int = 0,
    meta: dict[str, Any] | None = None,
    artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "rows": rows,
        "omitted": int(omitted),
        "meta": dict(meta or {}),
        "artifacts": dict(artifacts or {}),
    }


# --------------------------------------------------------- q-stage bridging
def _q_ctx(pc: PlanContext):
    from ctx import query

    query._load_registered_extensions()
    return query._Ctx(pc.ws, pc.store)


def _run_q_stage(pc: PlanContext, stage: str, argv: list[str], in_stream=None):
    from ctx import query

    query._load_registered_extensions()
    st = query.STAGES.get(stage)
    if st is None:
        raise OpError(f"q stage {stage!r} unavailable")
    stream = in_stream if in_stream is not None else query.Stream("start", [])
    out = st.fn(_q_ctx(pc), stream, argv)
    if len(out.rows) > st.row_cap:
        out.omitted += len(out.rows) - st.row_cap
        out.rows = out.rows[: st.row_cap]
    return out


def _stream_of(input_payload: dict[str, Any] | None):
    from ctx import query

    if input_payload is None:
        return query.Stream("start", [])
    return query.Stream(
        str(input_payload.get("kind") or "records"),
        [dict(r) for r in input_payload.get("rows") or []],
        omitted=int(input_payload.get("omitted") or 0),
    )


def _need(args: dict[str, Any], key: str) -> str | None:
    v = args.get(key)
    if not isinstance(v, str) or not v:
        return f"args.{key} (string) is required"
    return None


# ------------------------------------------------------------------ sources
_CHANGED_DERIVE_CAP = 64


def _op_repo_changed(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    """Changed files this generation, derived into the facts store so the
    temporal plane is queryable by later joins. Also derives decl/imp
    facts (skeletons) for the changed files themselves — that is what
    upgrades the root-cause join from file-level to symbol-precise."""
    from ctx import facts

    files = facts.changed_files_snapshot(pc.ws)
    derived = facts.derive_generation(pc.ws, store=pc.store)
    gen = derived.get("generation")
    pc.generation = pc.generation or gen
    decls = 0
    for rel in files[:_CHANGED_DERIVE_CAP]:
        d = facts.derive_file(pc.store, pc.ws, rel)
        decls += int(d.get("decl", 0) or 0)
    meta: dict[str, Any] = {"engine": "git", "generation": gen, "decl_facts": decls}
    if gen is None:
        meta["note"] = "no generation (non-git workspace or git unavailable)"
    return payload("files", [{"file": f} for f in files], meta=meta)


def _op_repo_inventory(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    from ctx.repomap import repo_map

    budget = int(args.get("budget", 400) or 400)
    out = repo_map(pc.store, pc.ws, budget=max(100, budget), focus=args.get("focus"))
    return payload("text", [{"text": out}], meta={"engine": "repomap"})


def _op_code_search(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    argv = [str(args.get("pattern") or "")]
    if args.get("glob"):
        argv += ["--glob", str(args["glob"])]
    out = _run_q_stage(pc, "search", argv)
    return payload("sites", out.rows, omitted=out.omitted, meta={"engine": "builtin-regex"})


def _op_code_refs(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    out = _run_q_stage(pc, "refs", [str(args.get("symbol") or "")])
    try:
        from ctx.codeverbs import _select_engine

        engine = _select_engine()
    except Exception:
        engine = "ast"
    return payload("sites", out.rows, omitted=out.omitted, meta={"engine": engine})


def _mk_callgraph_op(stage: str):
    def op(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
        argv = [str(args.get("symbol") or "")]
        if stage == "impact" and args.get("depth"):
            argv += ["--depth", str(int(args["depth"]))]
        out = _run_q_stage(pc, stage, argv)
        return payload(
            "sites", out.rows, omitted=out.omitted,
            meta={"engine": "ast · name-resolved"},
        )

    return op


def _op_ast_search(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    from ctx import astgrep

    rows, meta = astgrep.ast_search(
        pc.ws,
        pc.store,
        str(args.get("pattern") or ""),
        language=args.get("language"),
        glob=args.get("glob"),
        cap=int(args.get("cap", DEFAULT_ROW_CAP) or DEFAULT_ROW_CAP),
    )
    omitted = max(0, int(meta.pop("matched", len(rows))) - len(rows))
    return payload("sites", rows, omitted=omitted, meta=meta)


def _op_evidence_join(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    from ctx import facts

    on = str(args.get("on") or "")
    joins = {
        "failing_in_changed": lambda: facts.failing_in_changed(
            pc.ws, pc.store, args.get("generation"), run=args.get("run")
        ),
        "untouched_failures": lambda: facts.untouched_failures(
            pc.ws, pc.store, args.get("generation"), run=args.get("run")
        ),
        "shared_cause_groups": lambda: facts.shared_cause_groups(
            pc.ws, pc.store, run=args.get("run")
        ),
        "symbol_neighbors": lambda: facts.symbol_neighbors(
            pc.ws, pc.store, str(args.get("symbol") or "")
        ),
    }
    fn = joins.get(on)
    if fn is None:
        raise OpError(f"unknown join {on!r}; known: {', '.join(sorted(joins))}")
    rows = fn()
    return payload("records", rows, meta={"engine": "facts.sqlite", "join": on})


def _check_join_args(args: dict) -> str | None:
    on = args.get("on")
    known = ("failing_in_changed", "untouched_failures", "shared_cause_groups", "symbol_neighbors")
    if on not in known:
        return f"args.on must be one of {known}"
    if on == "symbol_neighbors":
        return _need(args, "symbol")
    return None


def _op_q_pipe(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    from ctx import query

    query._load_registered_extensions()
    parsed = query.parse_query(str(args.get("query") or ""))
    stream = query.Stream("start", [])
    qc = _q_ctx(pc)
    for name, argv in parsed:
        st = query.STAGES[name]
        stream = st.fn(qc, stream, argv)
        if len(stream.rows) > st.row_cap:
            stream.omitted += len(stream.rows) - st.row_cap
            stream.rows = stream.rows[: st.row_cap]
    public = [{k: v for k, v in r.items() if not k.startswith("_")} for r in stream.rows]
    return payload(
        stream.kind, public, omitted=stream.omitted,
        meta={"engine": "q", "stages": len(parsed)},
    )


# --------------------------------------------------------------- transforms
def _op_ast_outline(pc: PlanContext, args: dict, inp: dict | None) -> dict[str, Any]:
    from ctx.skeleton import skeleton_for, skeleton_outline

    rows_in = (inp or {}).get("rows") or []
    files: list[str] = []
    for r in rows_in:
        f = str(r.get("file") or "")
        if f and f not in files:
            files.append(f)
    cap = int(args.get("cap", OUTLINE_FILE_CAP) or OUTLINE_FILE_CAP)
    take, omitted = files[:cap], max(0, len(files) - cap)
    rows: list[dict[str, Any]] = []
    parsers: list[str] = []
    budget = pc.ws.config.budgets.digest_tokens
    for rel in take:
        try:
            sk = skeleton_for(pc.store, pc.ws, rel)
            parsers.append(str(sk.get("parser") or "?"))
            rows.append({"file": rel, "text": skeleton_outline(sk, budget)})
        except Exception as e:
            rows.append({"file": rel, "text": f"[outline repo:{rel}] unavailable ({e})"})
    engine = sorted(set(parsers))
    return payload(
        "text", rows, omitted=omitted,
        meta={"engine": "+".join(engine) if engine else "none"},
    )


def _op_related_tests(pc: PlanContext, args: dict, inp: dict | None) -> dict[str, Any]:
    """Test files plausibly covering the input files: path heuristic
    (test_<stem> / <stem>_test) plus the facts import edge when present.
    Precision labeled per row — this is triage, not a coverage map."""
    from pathlib import PurePosixPath

    rows_in = (inp or {}).get("rows") or []
    stems: list[str] = []
    for r in rows_in:
        f = str(r.get("file") or "")
        if f:
            stem = PurePosixPath(f).stem
            if stem and stem not in stems:
                stems.append(stem)
    out_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Path heuristic over the repo tree (bounded via the retrieval walk).
    from ctx.refs import parse_ref
    from ctx.retrieval import _resolve_repo_targets

    targets, _, _ = _resolve_repo_targets(
        pc.store, pc.ws, parse_ref("repo:"), glob="**/*test*.py", scope=None
    )
    for t in targets:
        rel = str(t.label)
        name = PurePosixPath(rel).stem
        for stem in stems:
            hit = None
            if name in (f"test_{stem}", f"{stem}_test"):
                hit = "path (test_<stem> convention)"
            elif f"import {stem}" in t.text or f"from {stem}" in t.text:
                hit = "imports the module (textual)"
            if hit and rel not in seen:
                seen.add(rel)
                out_rows.append({"file": rel, "covers": stem, "precision": hit})
                break
    out_rows.sort(key=lambda r: (r["file"], r["covers"]))
    return payload("files", out_rows, meta={"engine": "heuristic"})


def _mk_combinator_op(stage: str, arg_keys: tuple[str, ...]):
    def op(pc: PlanContext, args: dict, inp: dict | None) -> dict[str, Any]:
        argv = [str(args[k]) for k in arg_keys if args.get(k) is not None]
        out = _run_q_stage(pc, stage, argv, in_stream=_stream_of(inp))
        public = [{k: v for k, v in r.items() if not k.startswith("_")} for r in out.rows]
        return payload(out.kind, public, omitted=out.omitted, meta={"engine": "q"})

    return op


# ------------------------------------------------------------ execute class
def _op_test_run(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    """Birth-gate capture of a test command; failing-test census as rows.
    The full run stays addressable as ``run:<id>`` — drill-down is
    retrieval, never re-execution."""
    from ctx import facts
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    command = str(args.get("command") or "")
    timeout = float(args.get("timeout", pc.timeout) or pc.timeout)
    capture = run_capture(pc.ws, [command], shell=True, timeout=timeout, store=pc.store)
    _digest, manifest = render_run_digest(pc.store, pc.ws, capture.manifest, focus=None)
    short = str(manifest.get("id", "")).removeprefix("sha256:")[:12]
    facts.derive_generation(pc.ws, store=pc.store)
    derived = facts.derive_run(pc.store, pc.ws, manifest)
    run_id = str(derived.get("run") or short)
    rows = facts.fails_sites(pc.ws, pc.store, run=run_id)
    result = manifest.get("result") or {}
    outcome = "pass" if result.get("exitCode") == 0 else "fail"
    meta = {
        "engine": "run-capture",
        "outcome": outcome,
        "exit": result.get("exitCode"),
        "profile": (manifest.get("digest") or {}).get("profile"),
        "derived_fails": derived.get("fail", 0),
    }
    return payload("records", rows, meta=meta, artifacts={"run": f"run:{short}"})


def _op_rewrite_preview(pc: PlanContext, args: dict, _inp) -> dict[str, Any]:
    from ctx import astgrep

    rows, meta = astgrep.rewrite_preview(
        pc.ws,
        pc.store,
        str(args.get("pattern") or ""),
        str(args.get("rewrite") or ""),
        language=args.get("language"),
        glob=args.get("glob"),
    )
    artifacts = {}
    if meta.get("patch_blob"):
        artifacts["patch"] = f"blob:{meta['patch_blob']}"
    return payload("records", rows, meta=meta, artifacts=artifacts)


def _op_rewrite_apply(pc: PlanContext, args: dict, inp: dict | None) -> dict[str, Any]:
    from ctx import astgrep

    patch_blob = args.get("patch_blob")
    expect_gen = args.get("generation")
    if inp is not None:
        meta_in = inp.get("meta") or {}
        patch_blob = patch_blob or meta_in.get("patch_blob")
        expect_gen = expect_gen or meta_in.get("generation")
    rows, meta = astgrep.rewrite_apply(pc.ws, pc.store, str(patch_blob or ""), expect_gen)
    return payload("records", rows, meta=meta)


def _op_semantic(mode: str):
    def op(pc: PlanContext, args: dict, inp: dict | None) -> dict[str, Any]:
        from ctx import semgrep_engine

        paths = None
        if inp is not None:
            files = sorted({str(r.get("file") or "") for r in inp.get("rows") or []} - {""})
            paths = files or None
        rows, meta = semgrep_engine.scan(
            pc.ws,
            str(args.get("rules") or ""),
            paths=paths,
            cap=int(args.get("cap", DEFAULT_ROW_CAP) or DEFAULT_ROW_CAP),
        )
        if mode == "taint":
            rows = [r for r in rows if r.get("trace")] or rows
        meta["mode"] = mode
        return payload("records", rows, meta=meta)

    return op


# ------------------------------------------------------------- registration
def _check_pattern(args: dict) -> str | None:
    return _need(args, "pattern")


def _check_symbol(args: dict) -> str | None:
    return _need(args, "symbol")


def _check_rewrite(args: dict) -> str | None:
    return _need(args, "pattern") or _need(args, "rewrite")


register_op("repo.changed", _op_repo_changed, input_kinds=(), output_kind="files",
            cost="index", doc="changed files this generation (git porcelain → facts)")
register_op("repo.inventory", _op_repo_inventory, input_kinds=(), output_kind="text",
            cost="scan", doc="ranked budget-fitted codebase map")
register_op("code.search", _op_code_search, input_kinds=(), output_kind="sites",
            cost="scan", doc="regex over repo files (args: pattern, glob)",
            check_args=_check_pattern)
register_op("code.refs", _op_code_refs, input_kinds=(), output_kind="sites",
            cost="scan", doc="reference sites (jedi → ast fallback)",
            check_args=_check_symbol)
register_op("code.callers", _mk_callgraph_op("callers"), input_kinds=(),
            output_kind="sites", cost="index", doc="direct callers (ast call graph)",
            check_args=_check_symbol)
register_op("code.callees", _mk_callgraph_op("callees"), input_kinds=(),
            output_kind="sites", cost="index", doc="in-repo callees (ast call graph)",
            check_args=_check_symbol)
register_op("code.impact", _mk_callgraph_op("impact"), input_kinds=(),
            output_kind="sites", cost="index",
            doc="transitive callers, depth ≤ 6 (args: symbol, depth)",
            check_args=_check_symbol)
register_op("ast.search", _op_ast_search, input_kinds=(), output_kind="sites",
            cost="scan", cacheable=True,
            doc="structural metavariable search (ast-grep → ast-grep-py → labeled "
                "regex fallback; args: pattern, language, glob)",
            engine_hint="ast-grep (binary on PATH)", check_args=_check_pattern)
register_op("evidence.join", _op_evidence_join, input_kinds=(), output_kind="records",
            cost="index",
            doc="Angle-lite fact joins (args.on: failing_in_changed | "
                "untouched_failures | shared_cause_groups | symbol_neighbors)",
            check_args=_check_join_args)
register_op("q.pipe", _op_q_pipe, input_kinds=(), output_kind="records",
            cost="scan", doc="one ctx q pipeline as a node (args: query)",
            check_args=lambda a: _need(a, "query"))
register_op("ast.outline", _op_ast_outline,
            input_kinds=("files", "sites"), output_kind="text", cost="index",
            doc=f"skeleton outline per input file (cap {OUTLINE_FILE_CAP}; derived-blob cached)")
register_op("code.related_tests", _op_related_tests,
            input_kinds=("files", "sites", "records"), output_kind="files", cost="scan",
            doc="test files plausibly covering input files (labeled heuristic)")
register_op("evidence.where", _mk_combinator_op("where", ("cond",)),
            input_kinds=("sites", "files", "records", "text", "symbols"),
            output_kind="records", cost="index",
            doc="filter rows (args.cond: <field><op><value>, ops = != ~)",
            check_args=lambda a: _need(a, "cond"))
register_op("evidence.group", _mk_combinator_op("group", ("field",)),
            input_kinds=("sites", "files", "records", "text", "symbols"),
            output_kind="records", cost="index",
            doc="group rows and mint the group census (args.field)",
            check_args=lambda a: _need(a, "field"))
register_op("evidence.top", _mk_combinator_op("top", ("n",)),
            input_kinds=("sites", "files", "records", "text", "symbols"),
            output_kind="records", cost="index", doc="keep top N groups/rows (args.n)")
register_op("evidence.count", _mk_combinator_op("count", ()),
            input_kinds=("sites", "files", "records", "text", "symbols"),
            output_kind="records", cost="index", doc="row count or per-group counts")
register_op("test.run", _op_test_run, input_kinds=(), output_kind="records",
            klass="execute", cost="test", cacheable=False,
            doc="birth-gate test capture; failing census rows; run: handle attached "
                "(args: command, timeout)",
            check_args=lambda a: _need(a, "command"))


def _astgrep_available() -> bool:
    from ctx import astgrep

    return astgrep.available()


def _semgrep_available() -> bool:
    from ctx import semgrep_engine

    return semgrep_engine.available()


register_op("ast.rewrite.preview", _op_rewrite_preview, input_kinds=(),
            output_kind="records", klass="execute", cost="process", cacheable=False,
            on_missing_default="fail", probe_available=_astgrep_available,
            engine_hint="ast-grep (binary on PATH); no lossy fallback for rewrites",
            doc="mechanical rewrite as a previewed patch blob (args: pattern, rewrite, "
                "language, glob) — generation recorded for the apply guard",
            check_args=_check_rewrite)
register_op("ast.rewrite.apply", _op_rewrite_apply,
            input_kinds=("records",), output_kind="records",
            klass="execute", cost="process", cacheable=False,
            on_missing_default="fail", probe_available=_astgrep_available,
            engine_hint="ast-grep (binary on PATH)",
            doc="transactional generation-guarded apply of a previewed patch "
                "(input: the preview node, or args: patch_blob, generation)")
for _mode in ("search", "taint", "policy_scan"):
    register_op(f"semantic.{_mode}", _op_semantic(_mode),
                input_kinds=("sites", "files", "records"), input_optional=True,
                output_kind="records", cost="process", cacheable=True,
                on_missing_default="skip", probe_available=_semgrep_available,
                engine_hint="semgrep (pip extra [sem]); hermetic, local rules only",
                doc=f"semgrep {_mode} with a committed workspace-local rules file "
                    "(args: rules, cap); an input narrows the scan to its files "
                    "— absent engine ⇒ declared skip",
                check_args=lambda a: _need(a, "rules"))


def ops_census() -> str:
    """Deterministic op inventory (``ctx plan ops``): name, kinds, class,
    cost, engine hint — the discoverability surface a plan author reads."""
    lines = [f"[ctx plan ops · {len(OPS)} registered]"]
    for name in sorted(OPS):
        spec = OPS[name]
        kinds = "|".join(spec.input_kinds) if spec.input_kinds else "∅"
        avail = ""
        if spec.probe_available is not None:
            avail = " · available" if spec.probe_available() else " · NOT INSTALLED"
        lines.append(
            f"  {name} ({kinds}→{spec.output_kind}) · {spec.klass} · {spec.cost}"
            f"{avail}"
        )
        if spec.doc:
            lines.append(f"      {spec.doc}")
    return "\n".join(lines)


__all__ = [
    "OPS",
    "OpSpec",
    "OpError",
    "PlanContext",
    "register_op",
    "payload",
    "ops_census",
]
