"""``ctx q`` — the M-H composition algebra (docs/ALGEBRA.md).

A TOTAL pipeline language over typed record streams: stages joined by
``|``, no loops, no recursion, hard cap of 8 stages. Totality is by
construction — every query terminates and its cost is statically
boundable, which is exactly WHY this algebra is safe for the bounded MCP
tier later, where arbitrary-code ``ctx eval`` can never live. This wave
deliberately ships NO MCP wiring (prefix-asset churn); the CLI verb is
the only entry point.

Streams: a stream is a ``list[dict]`` of records with a declared kind —
``symbols | sites | files | records | text``. Every stage declares
``(input_kinds, output_kind)``; a mismatch fails fast with a one-line
teaching error listing the valid pipelines.

Registry contract (FROZEN — fact stages register against it):
``STAGES: dict[str, Stage]`` and ``register_stage(name, fn, *,
input_kinds, output_kind, doc)`` are module-level and importable.
Late-bound: ``ctx.facts`` (engineer C) registers its stages at import
time; ``run_query`` imports it lazily and fail-open, so ``ctx q`` works
when facts is absent.

Boundedness: every stage respects a per-stage row cap (default 200) with
declared omission. Emission rides the EDC spirit — bounded digest, a
REQUIRED census (never silent truncation), and the final result set
minted as a derived canonical-JSON blob whose ``blob:<id>`` rides the
header. Per-stage result blobs are the natural v2 (per-stage provenance
today is the ``--trace`` row ledger); deferred, declared.

Determinism: same repo state + same query ⇒ identical bytes (sorted
orderings everywhere; content-addressed provenance only).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable

from ctx.store import Store, canonical_json
from ctx.textutil import bounded, fmt_int
from ctx.workspace import Workspace

# ------------------------------------------------------------------ model
KINDS = ("symbols", "sites", "files", "records", "text")
SAME = "same"  # output_kind sentinel: combinator passes its input kind through

MAX_STAGES = 8  # hard totality cap — never raise without an MCP-tier review
DEFAULT_ROW_CAP = 200
GET_SITE_CAP = 24  # ``get`` fans out one bounded slice per site
OUTLINE_FILE_CAP = 12  # ``outline`` fans out one outline per file
RENDER_CAP = 100  # rows rendered inline; remainder declared + addressable
_LINE_CAP = 160


class QueryError(Exception):
    """One-line teaching error: what broke, and what would be valid."""


@dataclass
class Stream:
    """Typed record stream between stages."""

    kind: str
    rows: list[dict]
    omitted: int = 0  # rows dropped by declared caps anywhere upstream
    groups: list[tuple[str, int]] | None = None  # set by ``group``


@dataclass(frozen=True)
class Stage:
    """One registered verb. ``fn(qc, stream, args) -> Stream``.

    ``input_kinds == ()`` marks a source stage (must open the pipeline);
    ``output_kind == SAME`` passes the input kind through unchanged.
    """

    name: str
    fn: Callable
    input_kinds: tuple
    output_kind: str
    doc: str
    row_cap: int = DEFAULT_ROW_CAP


#: FROZEN registry (engineer C's facts.py registers fact stages here).
STAGES: dict[str, Stage] = {}


def register_stage(
    name: str,
    fn,
    *,
    input_kinds: tuple,
    output_kind: str,
    doc: str,
    row_cap: int = DEFAULT_ROW_CAP,
) -> None:
    """Register a pipeline stage. Module-level, importable, late-bound.

    ``input_kinds``: tuple of stream kinds accepted (``()`` = source stage,
    must be first). ``output_kind``: one of KINDS, or ``SAME`` for
    kind-preserving combinators.
    """
    if output_kind != SAME and output_kind not in KINDS:
        raise ValueError(f"unknown output kind {output_kind!r}; kinds: {KINDS}")
    for k in input_kinds:
        if k not in KINDS:
            raise ValueError(f"unknown input kind {k!r}; kinds: {KINDS}")
    STAGES[name] = Stage(name, fn, tuple(input_kinds), output_kind, doc, row_cap)


@dataclass
class _Ctx:
    ws: Workspace
    store: Store
    trace: list[str] = field(default_factory=list)


# ------------------------------------------------------------- teaching aid
def _kinds_label(st: Stage) -> str:
    if not st.input_kinds:
        return "∅"
    if tuple(st.input_kinds) == KINDS:
        return "any"
    return "|".join(st.input_kinds)


def _pipelines_help() -> str:
    parts = [
        f"{n}({_kinds_label(st)}→{st.output_kind})" for n, st in sorted(STAGES.items())
    ]
    return "valid: " + " · ".join(parts)


# ---------------------------------------------------------------- grammar
def parse_query(text: str) -> list[tuple[str, list[str]]]:
    """Split on ``|`` shlex-aware (quoted args survive). Returns
    [(stage_name, args), ...]. Validates count and stage existence and the
    kind chain — all before anything executes (fail fast, total)."""
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as e:
        raise QueryError(f"ctx q: unparseable query ({e})") from e

    stages: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "|":
            stages.append([])
        else:
            stages[-1].append(tok)
    if any(not s for s in stages) or not tokens:
        raise QueryError(
            "ctx q: empty stage; grammar: '<stage> [args] | <stage> [args] | …'"
        )
    if len(stages) > MAX_STAGES:
        raise QueryError(
            f"ctx q: {len(stages)} stages > max {MAX_STAGES} (totality bound — "
            "no loops, no recursion; split the query)"
        )

    parsed: list[tuple[str, list[str]]] = []
    kind = None  # None = start of pipeline
    for i, toks in enumerate(stages, start=1):
        name, args = toks[0], toks[1:]
        st = STAGES.get(name)
        if st is None:
            raise QueryError(
                f"ctx q: unknown stage {name!r} (stage {i}); "
                f"known: {', '.join(sorted(STAGES))}"
            )
        if not st.input_kinds:  # source stage
            if kind is not None:
                raise QueryError(
                    f"ctx q: {name!r} is a source stage and must open the "
                    f"pipeline (stage {i}); {_pipelines_help()}"
                )
            kind = st.output_kind
        else:
            if kind is None:
                raise QueryError(
                    f"ctx q: {name!r} needs an upstream {_kinds_label(st)} "
                    f"stream but opens the pipeline (stage {i}); {_pipelines_help()}"
                )
            if kind not in st.input_kinds:
                raise QueryError(
                    f"ctx q: stage {name!r} (stage {i}) needs "
                    f"{_kinds_label(st)}, got {kind}; {_pipelines_help()}"
                )
            kind = kind if st.output_kind == SAME else st.output_kind
        parsed.append((name, args))
    return parsed


# ------------------------------------------------------------ arg helpers
def _need_arg(args: list[str], stage: str, what: str) -> str:
    pos = [a for a in args if not a.startswith("--")]
    if not pos:
        raise QueryError(f"ctx q: stage {stage!r} needs {what}")
    return pos[0]


def _flag(args: list[str], name: str, default, cast=str):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            try:
                return cast(args[i + 1])
            except (TypeError, ValueError) as e:
                raise QueryError(f"ctx q: bad value for {name}: {args[i+1]!r}") from e
        raise QueryError(f"ctx q: {name} needs a value")
    return default


# ------------------------------------------------------------ source stages
def _stage_refs(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    symbol = _need_arg(args, "refs", "a <Symbol>")
    from ctx import codeverbs

    sites = None
    if codeverbs._select_engine() == "jedi":
        try:
            sites, _ = codeverbs._jedi_refs(qc.ws, symbol)
        except Exception:
            sites = None
    if sites is None:
        sites, _ = codeverbs._ast_refs(qc.store, qc.ws, symbol, None)
    uniq: dict[tuple[str, int], str] = {}
    for rel, line, text in sites:
        uniq.setdefault((rel, line), text)
    rows = [
        {"file": rel, "line": line, "text": text.strip()[:_LINE_CAP], "symbol": symbol}
        for (rel, line), text in sorted(uniq.items())
    ]
    return Stream("sites", rows)


def _callgraph(qc: _Ctx):
    from ctx import callgraph

    return callgraph, callgraph._load_graph(qc.store, qc.ws)


def _stage_callers(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    symbol = _need_arg(args, "callers", "a <Symbol>")
    cg, g = _callgraph(qc)
    name, targets = cg._resolve(g, symbol)
    rows = []
    if targets:
        for qual in g.in_edges.get(name, []):
            n = g.nodes.get(qual)
            if n is not None:
                rows.append({"file": n.rel, "line": n.lineno, "symbol": qual})
    rows.sort(key=lambda r: (r["file"], r["line"], r["symbol"]))
    return Stream("sites", rows)


def _stage_callees(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    symbol = _need_arg(args, "callees", "a <Symbol>")
    cg, g = _callgraph(qc)
    _, targets = cg._resolve(g, symbol)
    rows = []
    seen: set[str] = set()
    for t in targets:
        for callee in g.out_edges.get(t.qual, []):
            for n in g.defs_by_name.get(callee, []):
                if n.qual not in seen:
                    seen.add(n.qual)
                    rows.append({"file": n.rel, "line": n.lineno, "symbol": n.qual})
    rows.sort(key=lambda r: (r["file"], r["line"], r["symbol"]))
    return Stream("sites", rows)


def _stage_impact(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    symbol = _need_arg(args, "impact", "a <Symbol>")
    depth = max(1, min(_flag(args, "--depth", 6, int), 6))
    cg, g = _callgraph(qc)
    name, targets = cg._resolve(g, symbol)
    rows: list[dict] = []
    if targets:
        reached: dict[str, int] = {}
        frontier = {name}
        for d in range(1, depth + 1):
            nxt: set[str] = set()
            for nm in frontier:
                for caller in g.in_edges.get(nm, []):
                    if caller not in reached:
                        reached[caller] = d
                        nxt.add(caller.split(".")[-1])
            frontier = nxt
            if not frontier:
                break
        for qual, d in reached.items():
            n = g.nodes.get(qual)
            if n is not None:
                rows.append(
                    {"file": n.rel, "line": n.lineno, "symbol": qual, "depth": d}
                )
    rows.sort(key=lambda r: (r["depth"], r["file"], r["line"], r["symbol"]))
    return Stream("sites", rows)


def _stage_search(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    pattern = _need_arg(args, "search", "a <pattern>")
    glob = _flag(args, "--glob", None)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise QueryError(f"ctx q: invalid search pattern {pattern!r} ({e})") from e
    from ctx.refs import parse_ref
    from ctx.retrieval import _resolve_repo_targets

    targets, _, _ = _resolve_repo_targets(
        qc.store, qc.ws, parse_ref("repo:"), glob=glob, scope=None
    )
    rows = []
    for t in targets:  # targets arrive path-sorted
        for i, ln in enumerate(t.text.splitlines(), start=1):
            if rx.search(ln):
                rows.append({"file": t.label, "line": i, "text": ln.strip()[:_LINE_CAP]})
    return Stream("sites", rows)


# ------------------------------------------------------- transform stages
def _stage_files(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    counts: dict[str, int] = {}
    for r in stream.rows:
        f = str(r.get("file", ""))
        counts[f] = counts.get(f, 0) + 1
    rows = [
        {"file": f, "n": n}
        for f, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return Stream("files", rows, omitted=stream.omitted)


def _outline_text(qc: _Ctx, rel: str) -> str:
    try:  # engineer A's M-F skeleton tier, when present (fail-open otherwise)
        from ctx.skeleton import (  # type: ignore[import-not-found]
            skeleton_for,
            skeleton_outline,
        )

        sk = skeleton_for(qc.store, qc.ws, rel)
        txt = (
            skeleton_outline(sk, qc.ws.config.budgets.digest_tokens)
            if isinstance(sk, dict)
            else sk
        )
        if isinstance(txt, str) and txt:
            return txt
    except Exception:
        pass
    if rel.endswith(".py"):
        try:
            from ctx.retrieval import _stats_outline

            return _stats_outline(qc.store, qc.ws, rel)
        except Exception as e:
            return f"[outline repo:{rel}] unavailable ({e})"
    return f"[outline repo:{rel}] unavailable (non-python; skeleton tier absent)"


def _stage_outline(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    take = stream.rows[:OUTLINE_FILE_CAP]
    omitted = stream.omitted + (len(stream.rows) - len(take))
    rows = [
        {"file": str(r.get("file", "")), "text": _outline_text(qc, str(r.get("file", "")))}
        for r in take
    ]
    return Stream("text", rows, omitted=omitted)


def _stage_get(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    context = max(0, _flag(args, "--context", 2, int))
    from ctx.retrieval import Selector, get

    take = stream.rows[:GET_SITE_CAP]
    omitted = stream.omitted + (len(stream.rows) - len(take))
    rows = []
    for r in take:
        rel, line = str(r.get("file", "")), int(r.get("line", 1) or 1)
        a, b = max(1, line - context), line + context
        try:
            text = get(qc.store, qc.ws, f"repo:{rel}", Selector(lines=(a, b)))
        except Exception:
            omitted += 1
            continue
        rows.append({"file": rel, "line": line, "text": text})
    return Stream("text", rows, omitted=omitted)


# ------------------------------------------------------------- combinators
def _stage_group(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    fld = _need_arg(args, "group", "a <field>")
    sizes: dict[str, int] = {}
    for r in stream.rows:
        k = str(r.get(fld, ""))
        sizes[k] = sizes.get(k, 0) + 1
    order = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))
    rank = {k: i for i, (k, _) in enumerate(order)}
    rows = sorted(
        enumerate(stream.rows), key=lambda ir: (rank[str(ir[1].get(fld, ""))], ir[0])
    )
    out = Stream(stream.kind, [r for _, r in rows], omitted=stream.omitted)
    out.groups = order
    out_rows = out.rows
    for r in out_rows:
        r["_group"] = str(r.get(fld, ""))
    return out


def _stage_top(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    raw = _need_arg(args, "top", "an <N>")
    try:
        n = max(1, int(raw))
    except ValueError as e:
        raise QueryError(f"ctx q: top needs an integer, got {raw!r}") from e
    if stream.groups is not None:
        keep = {k for k, _ in stream.groups[:n]}
        rows = [r for r in stream.rows if r.get("_group") in keep]
        out = Stream(
            stream.kind, rows, omitted=stream.omitted + (len(stream.rows) - len(rows))
        )
        out.groups = stream.groups[:n]
        return out
    rows = stream.rows[:n]
    return Stream(
        stream.kind, rows, omitted=stream.omitted + (len(stream.rows) - len(rows))
    )


_WHERE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(!=|=|~)(.*)$")


def _stage_where(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    cond = _need_arg(args, "where", "a <field><op><value> (ops: = != ~)")
    m = _WHERE_RE.match(cond)
    if not m:
        raise QueryError(
            f"ctx q: bad where clause {cond!r}; grammar: <field><op><value> "
            "with op one of = != ~(substring)"
        )
    fld, op, val = m.group(1), m.group(2), m.group(3)
    if op == "=":
        keep = lambda r: str(r.get(fld, "")) == val  # noqa: E731
    elif op == "!=":
        keep = lambda r: str(r.get(fld, "")) != val  # noqa: E731
    else:
        keep = lambda r: val in str(r.get(fld, ""))  # noqa: E731
    rows = [r for r in stream.rows if keep(r)]
    out = Stream(stream.kind, rows, omitted=stream.omitted)
    out.groups = stream.groups
    return out


def _stage_count(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    if stream.groups is not None:
        rows = [{"group": k, "n": n} for k, n in stream.groups]
    else:
        rows = [{"n": len(stream.rows)}]
    return Stream("records", rows, omitted=stream.omitted)


# ------------------------------------------------------------ registration
register_stage("refs", _stage_refs, input_kinds=(), output_kind="sites",
               doc="refs <Symbol> — reference sites (codeverbs engine)")
register_stage("callers", _stage_callers, input_kinds=(), output_kind="sites",
               doc="callers <Symbol> — direct callers (ast call graph)")
register_stage("callees", _stage_callees, input_kinds=(), output_kind="sites",
               doc="callees <Symbol> — in-repo callees (ast call graph)")
register_stage("impact", _stage_impact, input_kinds=(), output_kind="sites",
               doc="impact <Symbol> [--depth N] — transitive callers, depth≤6")
register_stage("search", _stage_search, input_kinds=(), output_kind="sites",
               doc="search <pattern> [--glob G] — regex over repo files")
register_stage("files", _stage_files, input_kinds=("sites",), output_kind="files",
               doc="files — dedup sites to per-file counts")
register_stage("outline", _stage_outline, input_kinds=("files",), output_kind="text",
               doc=f"outline — priced symbol outline per file (cap {OUTLINE_FILE_CAP})")
register_stage("get", _stage_get, input_kinds=("sites",), output_kind="text",
               doc=f"get [--context N] — bounded slice per site (cap {GET_SITE_CAP})")
register_stage("group", _stage_group, input_kinds=KINDS, output_kind=SAME,
               doc="group <field> — order rows by group size, mint group census")
register_stage("top", _stage_top, input_kinds=KINDS, output_kind=SAME,
               doc="top <N> — keep top N groups (after group) or first N rows")
register_stage("where", _stage_where, input_kinds=KINDS, output_kind=SAME,
               doc="where <field><op><value> — filter rows (= != ~substring)")
register_stage("count", _stage_count, input_kinds=KINDS, output_kind="records",
               doc="count — row count, or per-group counts after group")


# ---------------------------------------------------------------- execution
def _load_registered_extensions() -> None:
    """Late binding: engineer C's facts.py registers fact stages at import.
    Fail-open by contract — q works when facts is absent or broken."""
    try:
        import ctx.facts  # noqa: F401
    except Exception:
        pass


def _row_line(kind: str, r: dict) -> str:
    if kind == "sites":
        what = str(r.get("text") or r.get("symbol") or "")
        depth = f" · depth {r['depth']}" if "depth" in r else ""
        return f"repo:{r.get('file','')}:L{r.get('line','?')}: {what}{depth}"
    if kind == "files":
        return f"{r.get('file','')} · {fmt_int(int(r.get('n', 0)))} sites"
    if kind == "symbols":
        return f"{r.get('symbol', r.get('name',''))}  {r.get('file','')}:{r.get('line','')}"
    # records (and any future kind): sorted key=value, private fields hidden
    return " · ".join(
        f"{k}={r[k]}" for k in sorted(r) if not k.startswith("_")
    )


def _render(
    ws: Workspace, store: Store, query: str, n_stages: int, out: Stream, trace: list[str] | None
) -> str:
    # Derived, addressable result set (v1-lite: final stream only; per-stage
    # blobs deferred — the --trace ledger is stage provenance today).
    public_rows = [
        {k: v for k, v in r.items() if not k.startswith("_")} for r in out.rows
    ]
    payload = {
        "format": "ctx.q/v1",
        "kind": out.kind,
        "query": query,
        "rows": public_rows,
        "omitted_upstream": out.omitted,
    }
    blob_id = store.put_blob(canonical_json(payload))
    short = blob_id[:12]

    lines = [f"[ctx q · {n_stages} stages · {out.kind} · blob:{short}]"]
    total = len(out.rows)
    shown_rows = out.rows[:RENDER_CAP]
    census = f"rows (census): {fmt_int(total)} · shown: {fmt_int(len(shown_rows))}"
    if out.omitted:
        census += (
            f" · capped: {fmt_int(out.omitted)} rows omitted upstream (declared; "
            "narrow with where/top)"
        )
    lines.append(census)
    if out.groups is not None:
        head = out.groups[:10]
        gline = "groups (census): " + " · ".join(f"{k or '∅'}:{n}" for k, n in head)
        if len(out.groups) > len(head):
            gline += f" · … +{fmt_int(len(out.groups) - len(head))} more groups"
        lines.append(gline)

    if out.kind == "text":
        for r in shown_rows:
            lines.append(str(r.get("text", "")))
    else:
        for r in shown_rows:
            lines.append(_row_line(out.kind, r))
    if total > len(shown_rows):
        lines.append(
            f"… +{fmt_int(total - len(shown_rows))} more rows · full set: "
            f"ctx get blob:{short} --json-pointer /rows"
        )
    if trace is not None:
        lines.append("trace:")
        lines.extend(f"  {t}" for t in trace)
    return "\n".join(lines)


def run_query(
    ws: Workspace, store: Store, text: str, *, trace: bool = False
) -> tuple[str, int]:
    """Execute one total pipeline. Returns (rendered, exit_code): 0 on
    success, 2 on a query error (the rendered text IS the teaching line)."""
    _load_registered_extensions()
    try:
        parsed = parse_query(text)
    except QueryError as e:
        return str(e), 2

    qc = _Ctx(ws, store)
    stream = Stream("start", [])
    try:
        for i, (name, args) in enumerate(parsed, start=1):
            st = STAGES[name]
            n_in = len(stream.rows)
            stream = st.fn(qc, stream, args)
            if len(stream.rows) > st.row_cap:
                stream.omitted += len(stream.rows) - st.row_cap
                stream.rows = stream.rows[: st.row_cap]
            qc.trace.append(
                f"{i} {' '.join([name, *args])} · in {n_in} → out {len(stream.rows)}"
                + (f" · omitted {stream.omitted}" if stream.omitted else "")
            )
    except QueryError as e:
        return str(e), 2

    rendered = _render(
        ws, store, text, len(parsed), stream, qc.trace if trace else None
    )
    # EDC-spirit emission backstop: the caller may re-bound under a plan;
    # library callers get a bounded digest either way.
    return bounded(rendered, ws.config.budgets.result_tokens), 0
