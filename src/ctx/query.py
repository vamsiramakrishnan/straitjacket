"""``ctx q`` — the M-H composition algebra (docs/ALGEBRA.md).

A TOTAL pipeline language over typed record streams: stages joined by
``|``, no loops, no recursion, hard cap of 8 stages. Totality is by
construction — every query terminates and its cost is statically
boundable, which is exactly WHY this algebra is safe for the bounded MCP
tier later, where arbitrary-code ``ctx py`` can never live. This wave
deliberately ships NO MCP wiring (prefix-asset churn); the CLI verb is
the only entry point.

Streams: a stream is a ``list[dict]`` of records with a declared kind —
``symbols | sites | files | records | text``. Every stage declares
``(input_kinds, output_kind)``; a mismatch fails fast with a one-line
teaching error listing the valid pipelines.

Registry contract (FROZEN — fact stages register against it):
``STAGES: dict[str, Stage]`` and ``register_stage(name, fn, *,
input_kinds, output_kind, doc, empty_hint=None)`` are module-level and
importable (``empty_hint`` is an additive keyword — see below).
Late-bound: ``ctx.facts`` (engineer C) registers its stages at import
time; ``run_query`` imports it lazily and fail-open, so ``ctx q`` works
when facts is absent.

Self-healing empty results (ALGEBRA-wide principle; debt fac2339eff — the
live A/B receipt: a pipeline whose empty result teaches nothing converts
to re-execution, 3 identical dry joins + 1 malformed before abandonment):

* A 0-row result is never rendered as a bare census. The renderer walks
  the per-stage trace (built unconditionally, ``--trace`` only controls
  printing it) and names WHERE the stream went empty and why-shaped
  guidance: ``0 rows after stage N (<name>): <hint>``. The hint comes
  from, in order: the ``Stream.note`` the stage attached at runtime (the
  facts note channel), the stage's static ``empty_hint`` registration
  keyword, else a generic per-position emptiness hint.
* Unknown-stage and kind-mismatch errors carry a did-you-mean line with
  concrete working pipelines (difflib close matches + stages present
  elsewhere in the query — the live failure ``'last | fails'`` inverted
  stage and argument).
* Dry-run guard rail: the workspace remembers (``.ctx-session-reads/
  q-dry.json``, fail-open, house ledger pattern) the last
  ``Q_DRY_REMEMBER`` pipeline texts that returned 0 rows this session; an
  IDENTICAL re-issue after a 0-row result gets a stronger banner naming
  the missing precondition while STILL executing (never blocks), and
  appends ``{"op": "q_dry_rerun", "pipeline": ...}`` to
  ``.ctx-session-reads/q-dry.jsonl`` (``ts`` operational-only) for the
  reflex plane. The banner is deterministic given the session's query
  sequence; the ledger dir is generation-excluded, so this state never
  perturbs generation hashing.

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

import difflib
import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from ctx.sessiondir import LEDGER_DIR_NAME, session_reads_path
from ctx.store import Store, canonical_json
from ctx.textutil import EVIDENCE_LINE_CHARS, bounded, fmt_int, short_id
from ctx.workspace import Workspace

# ------------------------------------------------------------------ model
KINDS = ("symbols", "sites", "files", "records", "text")
SAME = "same"  # output_kind sentinel: combinator passes its input kind through

# Closure lattice (docs/DIGEST-CLOSURE.md): the representation kinds are the
# bounded, digest-rate stream types; ``text`` is the sole terminal kind that
# carries raw byte payload. The single-refinement-boundary theorem holds
# because no stage maps TERMINAL_KIND back to a REPRESENTATION_KIND.
REPRESENTATION_KINDS = ("symbols", "sites", "files", "records")
TERMINAL_KIND = "text"

MAX_STAGES = 8  # hard totality cap — never raise without an MCP-tier review
DEFAULT_ROW_CAP = 200
GET_SITE_CAP = 24  # ``get`` fans out one bounded slice per site
OUTLINE_FILE_CAP = 12  # ``outline`` fans out one outline per file
RENDER_CAP = 100  # rows rendered inline; remainder declared + addressable


class QueryError(Exception):
    """One-line teaching error: what broke, and what would be valid."""


@dataclass
class Stream:
    """Typed record stream between stages.

    ``note`` (additive, default ``None`` — no existing-call breakage) is
    the runtime hint channel: a stage that KNOWS why it produced nothing
    (e.g. facts' ``fails`` with no captured run) attaches the missing
    precondition here; the empty-result diagnosis prefers it over the
    stage's static ``empty_hint``.
    """

    kind: str
    rows: list[dict]
    omitted: int = 0  # rows dropped by declared caps anywhere upstream
    groups: list[tuple[str, int]] | None = None  # set by ``group``
    note: str | None = None  # runtime empty-result hint (facts note channel)
    # Selection receipt (M-K2, additive): a source that SELECTS from a larger
    # population (``corpus``) attaches {considered, selected, engine, …} here;
    # the executor carries it through combinators and the renderer declares it.
    coverage: dict | None = None


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
    empty_hint: str | None = None  # static why-shaped hint for 0-row results

    @property
    def closure(self) -> str:
        """Digest-closure class, derived from the type signature alone
        (docs/DIGEST-CLOSURE.md). A stage is:

        * ``source``     — opens a pipeline (``input_kinds == ()``): lifts the
          fact store / repo into the bounded ``sites`` representation.
        * ``materialize`` — emits the terminal ``text`` kind: the single point
          where raw artifact bytes enter the stream (the priced refinement
          boundary). Design law: byte materialization MUST emit ``text``.
        * ``closed``     — representation → representation, computable at
          digest-rate without rehydrating raw bytes.

        The single-refinement-boundary theorem: no stage maps ``text`` back to
        a representation kind (``sites``/``files``/``symbols``), so a pipeline
        materializes bytes at most once, and only terminally. Enforced by
        ``tests/test_digest_closure.py``.
        """
        if not self.input_kinds:
            return "source"
        if self.output_kind == "text":
            return "materialize"
        return "closed"


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
    empty_hint: str | None = None,
) -> None:
    """Register a pipeline stage. Module-level, importable, late-bound.

    FROZEN contract surface, extended additively: the required keywords
    (``input_kinds``, ``output_kind``, ``doc``) are unchanged; existing
    registrations keep working verbatim.

    ``input_kinds``: tuple of stream kinds accepted (``()`` = source stage,
    must be first). ``output_kind``: one of KINDS, or ``SAME`` for
    kind-preserving combinators. ``empty_hint`` (optional, additive): a
    why-shaped teaching line used when a pipeline goes to 0 rows at this
    stage — name the likely missing precondition and the command that
    creates it. A ``Stream.note`` attached at runtime by the stage takes
    precedence over this static hint; with neither, the diagnosis falls
    back to a generic per-stage emptiness hint.
    """
    if output_kind != SAME and output_kind not in KINDS:
        raise ValueError(f"unknown output kind {output_kind!r}; kinds: {KINDS}")
    for k in input_kinds:
        if k not in KINDS:
            raise ValueError(f"unknown input kind {k!r}; kinds: {KINDS}")
    STAGES[name] = Stage(
        name, fn, tuple(input_kinds), output_kind, doc, row_cap, empty_hint
    )


def pipeline_closure(stage_names: "list[str] | tuple[str, ...]") -> str:
    """Static closure verdict for a pipeline (docs/DIGEST-CLOSURE.md).

    Returns ``"closed"`` when the whole pipeline runs at digest-rate (no byte
    materialization), else ``"refinement@<n>:<name>"`` naming the 1-indexed
    stage where bytes enter the stream. The prefix before that stage is always
    digest-closed — that is the theorem, not a heuristic."""
    for i, nm in enumerate(stage_names, start=1):
        st = STAGES.get(nm)
        if st is not None and st.closure == "materialize":
            return f"refinement@{i}:{nm}"
    return "closed"


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


#: Concrete working pipelines per stage, used by did-you-mean lines. Entries
#: for late-bound stages (facts) are inert until those stages register —
#: ``_stage_examples`` only emits examples whose every stage exists.
_EXAMPLES: dict[str, tuple[str, ...]] = {
    "refs": ("refs TokenBucket | group file | top 3 | get --context 5",),
    "callers": ("callers TokenBucket | files",),
    "callees": ("callees TokenBucket | files",),
    "impact": ("impact TokenBucket --depth 3",),
    "search": ("search TODO --glob 'src/*.py' | files",),
    "files": ("search TODO --glob 'src/*.py' | files",),
    "corpus": (
        "corpus --ext py --changed | outline",
        "corpus --glob 'src/**' --max 20",
    ),
    "records": ("records run:<id>#stdout --jsonl | group level | count",),
    "distinct": ("search TODO --glob 'src/*.py' | distinct file",),
    "histogram": ("search TODO --glob 'src/*.py' | histogram file",),
    "outline": ("search TODO --glob 'src/*.py' | files | outline",),
    "get": ("refs TokenBucket | get --context 5",),
    "group": ("search TODO --glob 'src/*.py' | group file | count",),
    "top": ("refs TokenBucket | group file | top 3",),
    "where": ("search TODO --glob 'src/*.py' | where file~src/",),
    "count": ("search TODO --glob 'src/*.py' | count",),
    # facts stages (engineer C) — live only once registered:
    "fails": ("fails last | in-changed", "fails last | shared-cause"),
    "in-changed": ("fails last | in-changed",),
    "decls": ("decls --kind class | count",),
    "shared-cause": ("fails last | shared-cause",),
}


def _stage_examples(name: str) -> tuple[str, ...]:
    """Working pipelines that use ``name``; only pipelines whose every
    stage is currently registered (registries are late-bound)."""
    out: list[str] = []
    for ex in _EXAMPLES.get(name, ()):
        heads = [seg.strip().split()[0] for seg in ex.split("|") if seg.strip()]
        if heads and all(h in STAGES for h in heads):
            out.append(ex)
    if not out and name in STAGES:
        st = STAGES[name]
        if not st.input_kinds:
            out.append(name)
        elif "sites" in st.input_kinds:
            out.append(f"search <pattern> | {name}")
        else:
            out.append(f"<{_kinds_label(st)} stage> | {name}")
    return tuple(out)


def _did_you_mean(name: str, all_stage_heads: list[str]) -> str:
    """The two most plausible correct pipelines for an unknown stage name:
    stages named elsewhere in the query first (the live failure
    ``'last | fails'`` inverted stage and argument), then difflib close
    matches over the registry. Empty string when nothing plausible."""
    candidates: list[str] = [h for h in all_stage_heads if h != name and h in STAGES]
    for m in difflib.get_close_matches(name, sorted(STAGES), n=2, cutoff=0.4):
        if m not in candidates:
            candidates.append(m)
    examples: list[str] = []
    for c in candidates:
        for ex in _stage_examples(c):
            if ex not in examples:
                examples.append(ex)
        if len(examples) >= 2:
            break
    if not examples:
        return ""
    return "did you mean: " + " · ".join(examples[:2])


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
            dym = _did_you_mean(name, [t[0] for t in stages])
            raise QueryError(
                f"ctx q: unknown stage {name!r} (stage {i}); "
                + (f"{dym}; " if dym else "")
                + f"known: {', '.join(sorted(STAGES))}"
            )
        example = _stage_examples(name)
        ex_tail = f"; example: {example[0]}" if example else ""
        if not st.input_kinds:  # source stage
            if kind is not None:
                raise QueryError(
                    f"ctx q: {name!r} is a source stage and must open the "
                    f"pipeline (stage {i}); {_pipelines_help()}{ex_tail}"
                )
            kind = st.output_kind
        else:
            if kind is None:
                raise QueryError(
                    f"ctx q: {name!r} needs an upstream {_kinds_label(st)} "
                    f"stream but opens the pipeline (stage {i}); "
                    f"{_pipelines_help()}{ex_tail}"
                )
            if kind not in st.input_kinds:
                raise QueryError(
                    f"ctx q: stage {name!r} (stage {i}) needs "
                    f"{_kinds_label(st)}, got {kind}; {_pipelines_help()}{ex_tail}"
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


def _multi_flag(args: list[str], name: str) -> list[str]:
    """All values of a repeatable ``--flag value`` pair, in order."""
    out: list[str] = []
    for i, a in enumerate(args):
        if a == name:
            if i + 1 >= len(args):
                raise QueryError(f"ctx q: {name} needs a value")
            out.append(args[i + 1])
    return out


# ------------------------------------------------------------ source stages
def _stage_refs(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    symbol = _need_arg(args, "refs", "a <Symbol>")
    from ctx import codeverbs

    # The shared engine ladder: SCIP (precise) → jedi → ast (docs/SUBSTRATE
    # §M-K4). The engine label rides the stream note so the code.refs op and
    # the digest can disclose which tier answered.
    sites, engine = codeverbs.resolve_refs(qc.store, qc.ws, symbol)
    uniq: dict[tuple[str, int], str] = {}
    for rel, line, text in sites:
        uniq.setdefault((rel, line), text)
    rows = [
        {"file": rel, "line": line, "text": text.strip()[:EVIDENCE_LINE_CHARS], "symbol": symbol}
        for (rel, line), text in sorted(uniq.items())
    ]
    out = Stream("sites", rows)
    out.note = f"engine: {engine}"
    return out


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
        # The session ledger is bookkeeping, never evidence (hook.py rule;
        # execution.py excludes it from generation hashing likewise) — and
        # since the q dry-run guard rail records pipeline texts there, a
        # ledger-scanning search would match its own guard state.
        if LEDGER_DIR_NAME in str(t.label).replace("\\", "/").split("/"):
            continue
        for i, ln in enumerate(t.text.splitlines(), start=1):
            m = rx.search(ln)
            if m:
                # Span-precise sites (M-K1): 1-based [col_a, col_b) character
                # columns of the first match on the line.
                rows.append(
                    {
                        "file": t.label,
                        "line": i,
                        "col_a": m.start() + 1,
                        "col_b": m.end() + 1,
                        "text": ln.strip()[:EVIDENCE_LINE_CHARS],
                    }
                )
    return Stream("sites", rows)


def _stage_corpus(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    """M-K2 ``file_select``: the bounded eligible file set, with a coverage
    receipt. Engines (git → fd → walk) live in ``ctx.filesets``."""
    from ctx import filesets

    rows, coverage, omitted = filesets.select(
        qc.ws,
        exts=_multi_flag(args, "--ext"),
        globs=_multi_flag(args, "--glob"),
        excludes=_multi_flag(args, "--exclude"),
        changed="--changed" in args,
        max_files=_flag(args, "--max", None, int),
    )
    out = Stream("files", rows, omitted=omitted)
    out.coverage = coverage
    if not rows and "--changed" in args:
        out.note = (
            "no changed files this generation — clean tree, or a non-git "
            "workspace (changed binds to generation facts, never mtime); "
            "drop --changed to select from the full corpus"
        )
    return out


def _json_pointer(doc, pointer: str):
    """RFC 6901, via the one implementation in :mod:`ctx.textutil`."""
    from ctx.textutil import json_pointer

    return json_pointer(doc, pointer)


def _records_rows(text: str, *, jsonl: bool, pointer: str | None) -> list[dict]:
    """Typed rows from a stored JSON/JSONL artifact. Dict items become rows
    verbatim; scalars/arrays are wrapped as ``{"value": item}``."""

    def _norm(items) -> list[dict]:
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = [items]
        return [
            it if isinstance(it, dict) else {"value": it}
            for it in items
        ]

    if jsonl:
        rows: list[dict] = []
        for i, ln in enumerate(text.splitlines(), start=1):
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError as e:
                raise QueryError(
                    f"ctx q: records --jsonl: line {i} is not JSON ({e.msg})"
                ) from e
            rows.extend(_norm(obj))
        return rows
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        # One labeled retry as JSONL before failing: tool output is often
        # JSON Lines without saying so.
        try:
            return _records_rows(text, jsonl=True, pointer=None)
        except QueryError:
            raise QueryError(
                "ctx q: records: artifact is neither a JSON document nor JSON "
                "Lines; for line-delimited streams pass --jsonl"
            ) from None
    if pointer:
        from ctx.textutil import JsonPointerError

        try:
            doc = _json_pointer(doc, pointer)
        except (JsonPointerError, KeyError, IndexError, ValueError) as e:
            raise QueryError(
                f"ctx q: records --pointer {pointer!r} does not resolve ({e})"
            ) from e
    return _norm(doc)


def _stage_records_src(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    """M-K3 ``record_transform`` source: open a stored artifact (run stream
    or blob) as the ``records`` kind — compiler/test/SARIF/lockfile JSON
    becomes queryable where it already lives, the store."""
    handle = _need_arg(args, "records", "a <handle> (run:<id>[#stream] | blob:<id>)")
    pointer = _flag(args, "--pointer", None)
    jsonl = "--jsonl" in args
    from ctx.refs import parse_ref

    try:
        ref = parse_ref(handle)
    except Exception as e:
        raise QueryError(f"ctx q: records: bad handle {handle!r} ({e})") from e
    from ctx._retrieval.targets import _resolve_run_targets, _stream_text

    try:
        if ref.kind == "blob":
            blob_id = qc.store.resolve_id(ref.id or "", kinds=("blob",))
            text = _stream_text(qc.store, blob_id)
        elif ref.kind == "run":
            targets, _skipped = _resolve_run_targets(qc.store, ref)
            if not targets:
                out = Stream("records", [])
                out.note = (
                    f"{handle} has no text streams — pick one with "
                    "#stdout/#stderr, or the run captured nothing"
                )
                return out
            text = targets[0].text
        else:
            raise QueryError(
                f"ctx q: records reads run:/blob: handles, got {ref.kind!r}"
            )
    except QueryError:
        raise
    except Exception as e:
        raise QueryError(f"ctx q: records: cannot resolve {handle!r} ({e})") from e
    return Stream("records", _records_rows(text, jsonl=jsonl, pointer=pointer))


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


def _stage_distinct(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    """M-K3: the unique values of one field, sorted — ``sort -u`` as a
    typed, closed stage."""
    fld = _need_arg(args, "distinct", "a <field>")
    vals = sorted({str(r.get(fld, "")) for r in stream.rows})
    return Stream("records", [{fld: v} for v in vals], omitted=stream.omitted)


_HISTOGRAM_BUCKETS = 10


def _stage_histogram(qc: _Ctx, stream: Stream, args: list[str]) -> Stream:
    """M-K3: value distribution of one field. All-numeric values get
    equal-width buckets; otherwise a categorical census sorted by
    (-count, value), capped at the bucket count with declared omission."""
    fld = _need_arg(args, "histogram", "a <field>")
    n_buckets = max(1, _flag(args, "--buckets", _HISTOGRAM_BUCKETS, int))
    raw = [str(r.get(fld, "")) for r in stream.rows]
    nums: list[float] | None
    try:
        nums = [float(v) for v in raw] if raw else None
    except ValueError:
        nums = None
    if nums:
        lo, hi = min(nums), max(nums)
        if lo == hi:
            rows = [{"bucket": format(lo, "g"), "n": len(nums)}]
            return Stream("records", rows, omitted=stream.omitted)
        width = (hi - lo) / n_buckets
        counts = [0] * n_buckets
        for v in nums:
            counts[min(n_buckets - 1, int((v - lo) / width))] += 1
        rows = [
            {
                "bucket": f"{format(lo + i * width, 'g')}–{format(lo + (i + 1) * width, 'g')}",
                "n": c,
            }
            for i, c in enumerate(counts)
        ]
        return Stream("records", rows, omitted=stream.omitted)
    sizes: dict[str, int] = {}
    for v in raw:
        sizes[v] = sizes.get(v, 0) + 1
    order = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))
    kept = order[:n_buckets]
    omitted = stream.omitted + sum(c for _, c in order[n_buckets:])
    rows = [{"bucket": k, "n": c} for k, c in kept]
    return Stream("records", rows, omitted=omitted)


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
register_stage("corpus", _stage_corpus, input_kinds=(), output_kind="files",
               doc="corpus [--ext E]… [--glob G]… [--exclude G]… [--changed] "
                   "[--max N] — bounded eligible file set (git → fd → walk)",
               empty_hint="no files selected — loosen --ext/--glob/--exclude, "
                          "or drop --changed on a clean tree")
register_stage("records", _stage_records_src, input_kinds=(), output_kind="records",
               doc="records <handle> [--jsonl] [--pointer /p] — stored JSON/JSONL "
                   "artifact as a typed record stream",
               empty_hint="the artifact parsed to zero records — check the "
                          "handle's stream (#stdout/#stderr) and --pointer")
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
register_stage("distinct", _stage_distinct, input_kinds=REPRESENTATION_KINDS,
               output_kind="records",
               doc="distinct <field> — unique values of a field, sorted")
register_stage("histogram", _stage_histogram, input_kinds=REPRESENTATION_KINDS,
               output_kind="records",
               doc="histogram <field> [--buckets N] — numeric buckets or "
                   "categorical census of a field")


# ---------------------------------------------------------------- execution
def _load_registered_extensions() -> None:
    """Late binding: engineer C's facts.py registers fact stages at import.
    Fail-open by contract — q works when facts is absent or broken."""
    try:
        import ctx.facts  # noqa: F401
    except Exception:
        pass


# ------------------------------------------- self-healing empty results
# Debt fac2339eff: an affordance whose empty result teaches nothing
# converts to re-execution (live A/B: 3 identical dry joins + 1 malformed
# variant, then abandonment). Empty results diagnose themselves; identical
# dry re-issues get a stronger banner but ALWAYS still execute.
Q_DRY_REMEMBER = 8  # last N 0-row pipeline texts remembered per session
_QDRY_STATE = "q-dry.json"
_QDRY_LEDGER = "q-dry.jsonl"


def _empty_diagnosis(
    stage_stats: list[tuple[str, int, int, str | None]],
) -> tuple[str, str] | None:
    """Walk the per-stage trace (always recorded; ``--trace`` only prints
    it) to the first stage whose output hit 0 rows. Returns
    ``(diagnosis_line, hint)`` or None when no stage went empty."""
    for i, (name, n_in, n_out, note) in enumerate(stage_stats, start=1):
        if n_out != 0:
            continue
        st = STAGES.get(name)
        hint = note or (st.empty_hint if st is not None else None)
        if not hint:
            doc = st.doc if st is not None else name
            if st is not None and not st.input_kinds:
                hint = (
                    "the source produced no rows — the evidence it reads may "
                    f"not exist yet; create it first ({doc})"
                )
            elif n_in:
                hint = (
                    f"all {fmt_int(n_in)} upstream rows were filtered out here "
                    f"— loosen this stage's arguments ({doc})"
                )
            else:
                hint = f"no rows reached this stage ({doc})"
        return f"0 rows after stage {i} ({name}): {hint}", hint
    return None


def _qdry_read(root) -> list[str]:
    """Last-N 0-row pipeline texts, oldest first. Fail-open: [] on any
    problem (the guard rail degrades to silence, never to an error)."""
    try:
        doc = json.loads(
            session_reads_path(root, _QDRY_STATE).read_text(encoding="utf-8")
        )
        dry = doc.get("dry") if isinstance(doc, dict) else None
        if isinstance(dry, list):
            return [str(p) for p in dry][-Q_DRY_REMEMBER:]
    except Exception:
        pass
    return []


def _qdry_write(root, dry: list[str]) -> None:
    """Atomic temp+rename write of the dry-pipeline state (house pattern:
    reflex.json). Fail-open — never raises."""
    try:
        d = session_reads_path(root)
        d.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"dry": dry[-Q_DRY_REMEMBER:]}, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".q-dry-")
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, str(d / _QDRY_STATE))
    except Exception:
        pass


def _qdry_ledger_append(root, pipeline: str) -> None:
    """One line per dry re-issue for the reflex plane. ``ts`` is
    operational-only (house rule: the ledger minus ts is a pure function
    of the session's query sequence). Fail-open — never raises."""
    try:
        d = session_reads_path(root)
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"op": "q_dry_rerun", "pipeline": pipeline, "ts": time.time()},
            sort_keys=True,
        )
        with open(d / _QDRY_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _qdry_note(root, pipeline: str, is_empty: bool, hint: str | None) -> str | None:
    """Apply the dry-run guard rail for one executed pipeline. Returns the
    stronger banner when this exact text just returned 0 rows and did so
    again — while the caller STILL emits the executed result (never
    blocks). Deterministic given the session's query sequence; every path
    fail-open."""
    try:
        dry = _qdry_read(root)
        if not is_empty:
            if pipeline in dry:
                _qdry_write(root, [p for p in dry if p != pipeline])
            return None
        banner = None
        if pipeline in dry:
            banner = (
                "this exact query returned 0 rows moments ago — the missing "
                f"precondition is: {hint or 'unknown (no stage hint)'}; "
                "re-running unchanged will not differ"
            )
            _qdry_ledger_append(root, pipeline)
        _qdry_write(root, [p for p in dry if p != pipeline] + [pipeline])
        return banner
    except Exception:
        return None


def _row_line(kind: str, r: dict) -> str:
    if kind == "sites":
        what = str(r.get("text") or r.get("symbol") or "")
        depth = f" · depth {r['depth']}" if "depth" in r else ""
        return f"repo:{r.get('file','')}:L{r.get('line','?')}: {what}{depth}"
    if kind == "files":
        if "n" in r:
            return f"{r.get('file','')} · {fmt_int(int(r.get('n', 0)))} sites"
        if "size" in r:
            return f"{r.get('file','')} · {fmt_int(int(r.get('size', 0)))} B"
        return str(r.get("file", ""))
    if kind == "symbols":
        return f"{r.get('symbol', r.get('name',''))}  {r.get('file','')}:{r.get('line','')}"
    # records (and any future kind): sorted key=value, private fields hidden
    return " · ".join(
        f"{k}={r[k]}" for k in sorted(r) if not k.startswith("_")
    )


def _render(
    ws: Workspace,
    store: Store,
    query: str,
    n_stages: int,
    out: Stream,
    trace: list[str] | None,
    empty_diag: str | None = None,
    dry_banner: str | None = None,
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
    if out.coverage:
        payload["coverage"] = out.coverage
    blob_id = store.put_blob(canonical_json(payload))
    short = short_id(blob_id)

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
    if out.coverage:
        cov = out.coverage
        seg = (
            f"coverage: considered {fmt_int(int(cov.get('considered', 0)))} · "
            f"selected {fmt_int(int(cov.get('selected', 0)))} · "
            f"engine {cov.get('engine', '?')}"
        )
        if cov.get("generation"):
            seg += f" · gen {cov['generation']}"
        lines.append(seg)
    # Self-healing emptiness: a 0-row result is never a bare census — the
    # per-stage diagnosis (and, on an identical dry re-issue, the stronger
    # banner) is evidence, not a suggestion; it must survive the
    # engagement filter, so it is emitted as plain lines, never under a
    # "next:" affordance block.
    if total == 0 and empty_diag:
        lines.append(empty_diag)
    if dry_banner:
        lines.append(dry_banner)
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
    # Per-stage trace, ALWAYS recorded (the --trace flag only prints it):
    # the empty-result diagnosis walks this unconditionally.
    stage_stats: list[tuple[str, int, int, str | None]] = []
    try:
        for i, (name, args) in enumerate(parsed, start=1):
            st = STAGES[name]
            n_in = len(stream.rows)
            prev_coverage = stream.coverage
            stream = st.fn(qc, stream, args)
            if stream.coverage is None and prev_coverage is not None:
                stream.coverage = prev_coverage  # selection receipt survives
            if len(stream.rows) > st.row_cap:
                stream.omitted += len(stream.rows) - st.row_cap
                stream.rows = stream.rows[: st.row_cap]
            stage_stats.append((name, n_in, len(stream.rows), stream.note))
            qc.trace.append(
                f"{i} {' '.join([name, *args])} · in {n_in} → out {len(stream.rows)}"
                + (f" · omitted {stream.omitted}" if stream.omitted else "")
            )
    except QueryError as e:
        return str(e), 2

    # Self-healing empty results (debt fac2339eff): diagnose WHERE the
    # stream went empty, and arm the dry-run guard rail — an identical
    # re-issue after a 0-row result banners the missing precondition but
    # STILL executes (never blocks).
    diag = _empty_diagnosis(stage_stats) if not stream.rows else None
    dry_banner = _qdry_note(
        ws.root, text.strip(), not stream.rows, diag[1] if diag else None
    )
    rendered = _render(
        ws,
        store,
        text,
        len(parsed),
        stream,
        qc.trace if trace else None,
        empty_diag=diag[0] if diag else None,
        dry_banner=dry_banner,
    )
    # EDC-spirit emission backstop: the caller may re-bound under a plan;
    # library callers get a bounded digest either way.
    return bounded(rendered, ws.config.budgets.result_tokens), 0
