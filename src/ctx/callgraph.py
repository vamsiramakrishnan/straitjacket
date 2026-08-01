"""Deterministic call graph over the workspace (``ctx callers/callees/impact/impls``).

`ctx callers`/`callees`/`impact` answer the questions that turn a multi-turn
grep-and-read trace into one query: who calls this, what does it call, and
what is transitively reachable (blast radius). `ctx impls` answers the fourth:
what implements or extends this type. Nodes we already had via ``ctx def``/
``refs``; this module adds the *edges*.

Engine ladder (CONTRIBUTING §1 — every rung optional, the bottom rung always
ships, and the rung in force is disclosed in the header of every answer):

    nodes      skeleton/tree-sitter (``[code]``)   → stdlib ``ast`` (python)
    call sites ast-grep ``$F($$$A)`` (``[code]``)  → stdlib ``ast`` (python)
    scoping    grimp import graph (``[map]``)      → per-file import names
    traversal  networkx (``[map]``)                → stdlib BFS

Nothing here is hand-rolled that a declared dependency already does: the
polyglot node set is ``ctx.skeleton``'s (20 extensions, content-cached), the
polyglot call sites are one ast-grep pattern over the 16 languages
``ctx.astgrep`` already maps, and the import graph is the one
``ctx.repomap._grimp_edges`` already resolves (namespace packages, relative
imports, src-layouts). v1 re-parsed Python with stdlib ``ast`` and stopped
there; v2 keeps that path as the always-available fallback only.

**Resolution is SCOPED, and its confidence is on every edge.** v1 bound a
call to ``foo`` to every in-repo ``def foo``, which on this repo put 15% of
definitions behind a colliding name and reported ``hosts.detect_all`` as a
caller of ``LogTemplateProfile.detect``. A call site now resolves in tiers and
takes the first non-empty one:

    local   a definition of that name in the calling file
    import  a definition in a file the caller DIRECTLY imports
    repo    a definition anywhere (v1 behaviour — the last resort)

``local`` and ``import`` are precise enough to state plainly; ``repo`` edges
are counted and labelled ``unscoped`` in the output, never silently mixed in.
A tier that yields several candidates is ambiguous and every candidate is
reported (SPEC §8) — the answer is never narrowed by an invisible tie-break.

Edges are keyed by the resolved **qualified** target, not by the unqualified
name, so ``ctx callers Class.method`` answers the question it was asked. v1
keyed ``in_edges`` by unqualified name, so a dotted query silently degraded to
the bare-name answer *and* suppressed the ambiguity note (because narrowing to
one target is what the note was conditioned on).

Caching is per file, keyed by that file's own stat fingerprint, so editing one
file re-parses one file. v1 keyed one blob on the whole corpus, so any edit
cost a full rebuild — measured on this repo at 170 ms warm against 1,754 ms
after a single-file touch, on the hot path of an agent that edits constantly.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

from ctx.store import Store
from ctx.textutil import fmt_int
from ctx.workspace import Workspace, stat_fingerprint

_FORMAT = "ctx.callgraph/v2"
_MAX_FILES = 5000
_MAX_DEPTH = 6  # hard bound on impact/reachability recursion
_MAX_ROWS = 20  # per-section row cap; the remainder is declared, never dropped

# Resolution tiers, best first. The first tier with any candidate wins; the
# label rides on every edge so a low-confidence answer can never be read as a
# precise one.
_TIER_LOCAL = "local"
_TIER_IMPORT = "import"
_TIER_REPO = "repo"
_TIERS = (_TIER_LOCAL, _TIER_IMPORT, _TIER_REPO)

# Path segments whose callers are exercise, not use. Grouped separately rather
# than dropped: on this repo 26 of 37 `callers put_blob` rows were tests,
# interleaved alphabetically with the 11 production callers that were the
# actual answer.
_NON_PRODUCTION = ("tests/", "test/", "evals/", "benchmarks/", "examples/")


class CallGraphError(Exception):
    pass


@dataclass
class _Def:
    qual: str  # ClassName.method or function
    rel: str
    lineno: int
    end: int
    kind: str = "function"  # function | method | class | …
    bases: list[str] = field(default_factory=list)  # class bases, by name
    lang: str = "python"


@dataclass
class _Unit:
    """One file's extraction — the unit of caching."""

    rel: str
    lang: str
    engine: str
    defs: list[_Def] = field(default_factory=list)
    # (enclosing qual, callee name, call-site line)
    calls: list[tuple[str, str, int]] = field(default_factory=list)
    # module-ish import targets as written, for the fallback scoping tier
    imports: list[str] = field(default_factory=list)


@dataclass
class _Graph:
    nodes: dict[str, _Def] = field(default_factory=dict)  # qual -> def
    defs_by_name: dict[str, list[str]] = field(default_factory=dict)  # name -> quals
    # resolved callee qual -> [(caller qual, call-site line, tier)]
    in_edges: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)
    # caller qual -> [(callee name, line, resolved target quals, tier)]
    out_edges: dict[str, list[tuple[str, int, list[str], str]]] = field(default_factory=dict)
    # base qual -> [subclass quals]
    subclasses: dict[str, list[str]] = field(default_factory=dict)
    engines: dict[str, str] = field(default_factory=dict)  # stage -> engine label


# --------------------------------------------------------------- extraction
def _py_bases(node: ast.ClassDef) -> list[str]:
    """Base names as written (``Profile``, ``base.Profile`` → ``Profile``).
    Resolution to a definition happens in the link phase, through the same
    tier ladder call sites use — a base is just another cross-file name."""
    out: list[str] = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            out.append(b.id)
        elif isinstance(b, ast.Attribute):
            out.append(b.attr)
    return out


class _PyVisitor(ast.NodeVisitor):
    """Definitions, their bases, and every call site with its line."""

    def __init__(self, unit: _Unit) -> None:
        self.unit = unit
        self.stack: list[str] = []

    def _qual(self) -> str:
        return ".".join(self.stack)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.unit.imports.append(a.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.unit.imports.append(node.module)
            # `from pkg.mod import name` also binds `name`; record the dotted
            # form so the link phase can match either spelling.
            for a in node.names:
                self.unit.imports.append(f"{node.module}.{a.name}")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        qual = self._qual()
        end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
        d = _Def(qual, self.unit.rel, node.lineno, end, "class", _py_bases(node))
        self.unit.defs.append(d)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node) -> None:  # noqa: ANN001
        self._function(node)

    def visit_AsyncFunctionDef(self, node) -> None:  # noqa: ANN001
        self._function(node)

    def _function(self, node) -> None:  # noqa: ANN001
        self.stack.append(node.name)
        qual = self._qual()
        end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
        kind = "method" if len(self.stack) > 1 else "function"
        self.unit.defs.append(_Def(qual, self.unit.rel, node.lineno, end, kind))
        # Calls lexically inside this function only — nested defs get their own
        # node and own edges. Every call site keeps its line: v1 reported the
        # caller's definition range, so seeing the actual call cost another read.
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node:
                continue
            if isinstance(child, ast.Call):
                name = _callee_name(child.func)
                if name:
                    self.unit.calls.append((qual, name, int(child.lineno)))
        self.generic_visit(node)
        self.stack.pop()


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr  # method/attr tail: self.foo() -> "foo"
    return None


def _unit_python(rel: str, source: str) -> _Unit:
    """stdlib ``ast`` — the rung that is always available (CONTRIBUTING §1)."""
    unit = _Unit(rel=rel, lang="python", engine="ast")
    tree = ast.parse(source)
    _PyVisitor(unit).visit(tree)
    # Deduplicate call sites per (caller, name, line) while keeping order.
    seen: set[tuple[str, str, int]] = set()
    calls: list[tuple[str, str, int]] = []
    for c in unit.calls:
        if c not in seen:
            seen.add(c)
            calls.append(c)
    unit.calls = calls
    return unit


def _astgrep_calls(source: str, lang: str) -> list[tuple[str, int]]:
    """(callee name, line) for every call site, via one ast-grep pattern.

    ``$F($$$A)`` matches a call in every language ast-grep supports, so the
    polyglot tier needs no per-grammar walker — the same pattern that finds
    ``helper(1)`` in Rust finds it in Go, TypeScript and Python. The callee
    text is reduced to its tail (``self.other`` → ``other``, ``pkg::f`` → ``f``)
    to match the name a definition is recorded under.
    """
    from ast_grep_py import SgRoot

    out: list[tuple[str, int]] = []
    root = SgRoot(source, lang).root()
    for hit in root.find_all(pattern="$F($$$A)"):
        m = hit.get_match("F")
        if m is None:
            continue
        text = m.text().strip()
        # Tail of a qualified callee: a.b.c / a::b / a->b all name `c`/`b`.
        for sep in ("::", "->", "."):
            if sep in text:
                text = text.rsplit(sep, 1)[-1]
        text = text.strip()
        if text.isidentifier():
            out.append((text, hit.range().start.line + 1))
    return out


def _unit_polyglot(store: Store, ws: Workspace, rel: str, lang: str) -> _Unit:
    """Nodes from ``ctx.skeleton`` (tree-sitter, content-cached), call sites
    from ast-grep. Both are already-declared engines; neither is re-implemented
    here. Raises when either is unavailable so the caller can degrade."""
    from ctx.astgrep import _lib_lang
    from ctx.skeleton import skeleton_for

    ag_lang = _lib_lang(rel)
    if ag_lang is None:
        raise CallGraphError(f"no ast-grep language for {rel}")
    sk = skeleton_for(store, ws, rel)
    unit = _Unit(rel=rel, lang=lang, engine=f"skeleton+astgrep/{sk.get('parser', '?')}")
    unit.imports = [str(i) for i in (sk.get("imports") or [])]

    # Definition ranges, innermost first, so a call site is attributed to the
    # tightest enclosing definition (a method, not its class).
    ranges: list[tuple[int, int, str]] = []
    for s in sk.get("symbols") or []:
        rng = s.get("range") or [0, 0]
        name, scope = str(s.get("name") or ""), s.get("scope")
        if not name:
            continue
        qual = f"{scope}.{name}" if scope else name
        kind = str(s.get("kind") or "function")
        d = _Def(qual, rel, int(rng[0]), int(rng[1]), kind, [], lang)
        unit.defs.append(d)
        ranges.append((int(rng[0]), int(rng[1]), qual))
    ranges.sort(key=lambda t: (t[1] - t[0], t[0]))  # narrowest first

    source = (ws.root / rel).read_text(encoding="utf-8", errors="replace")
    for name, line in _astgrep_calls(source, ag_lang):
        for a, b, qual in ranges:
            if a <= line <= b:
                unit.calls.append((qual, name, line))
                break
    return unit


def _extract_unit(store: Store, ws: Workspace, rel: str) -> _Unit | None:
    """One file → one unit, best available engine, absence never an error."""
    from ctx.skeleton import language_for

    lang = language_for(rel) or ("python" if rel.endswith(".py") else None)
    if lang is None:
        return None
    if lang != "python":
        if os.environ.get("CTX_CALLGRAPH_ENGINE") == "ast":
            return None  # python-only mode: the v1 corpus, for A/B and tests
        try:
            return _unit_polyglot(store, ws, rel, lang)
        except Exception:
            return None  # optional rung absent or grammar refused — skip file
    try:
        source = (ws.root / rel).read_text(encoding="utf-8", errors="replace")
        return _unit_python(rel, source)
    except (OSError, SyntaxError, ValueError):
        return None


# ------------------------------------------------------------------ scoping
def _import_edges(ws: Workspace, rels: set[str], units: dict[str, _Unit]) -> tuple[
    dict[str, set[str]], str
]:
    """file -> files it DIRECTLY imports, plus the engine label.

    The two sources UNION rather than compete. ``ctx.repomap._grimp_edges``
    already resolves namespace packages, relative imports and src-layouts —
    there is no reason to write that twice — but grimp models *importable
    packages*, so a `tests/` tree with no ``__init__.py``, a loose script, or
    any polyglot file is invisible to it. Taking grimp alone demoted every
    test-file caller to the unscoped tier (35 of 36 on this repo, all of them
    legitimate). The stem matcher covers what grimp cannot see; where both
    speak, grimp's answer is already included.

    Direct, not transitive: on this repo ``ctx.hosts`` transitively reaches
    ``ctx.digest.logprof`` through installer→hook→digest, so only the direct
    relation discriminates.
    """
    edges: dict[str, set[str]] = {}
    engines: list[str] = []
    try:
        from ctx.repomap import _grimp_edges

        for rel, targets in _grimp_edges(ws, rels).items():
            edges.setdefault(rel, set()).update(targets)
        if edges:
            engines.append("grimp")
    except Exception:
        pass

    # Map each recorded import string onto a known file by dotted stem,
    # walking the dotted name leftwards so `pkg.mod.name` finds `pkg/mod.py`.
    by_stem: dict[str, str] = {}
    for rel in rels:
        stem = rel.removesuffix(".py").removesuffix("/__init__")
        by_stem.setdefault(stem.replace("/", "."), rel)
    for rel, unit in units.items():
        for imp in unit.imports:
            dotted = imp.replace("::", ".").strip()
            while dotted:
                target = by_stem.get(dotted)
                if target and target != rel:
                    edges.setdefault(rel, set()).add(target)
                    break
                if "." not in dotted:
                    break
                dotted = dotted.rsplit(".", 1)[0]
    engines.append("imports")
    return edges, "+".join(engines)


def _resolve_name(
    name: str,
    caller_rel: str,
    g: _Graph,
    imports_of: dict[str, set[str]],
) -> tuple[list[str], str]:
    """(target quals, tier) for a name referenced from ``caller_rel``.

    The first tier with any candidate wins. Every candidate in that tier is
    returned — narrowing a tie invisibly is the failure mode SPEC §8 forbids.
    """
    cands = g.defs_by_name.get(name)
    if not cands:
        return [], _TIER_REPO
    local = [q for q in cands if g.nodes[q].rel == caller_rel]
    if local:
        return local, _TIER_LOCAL
    imported = imports_of.get(caller_rel) or set()
    if imported:
        scoped = [q for q in cands if g.nodes[q].rel in imported]
        if scoped:
            return scoped, _TIER_IMPORT
    return list(cands), _TIER_REPO


def _link(ws: Workspace, units: dict[str, _Unit]) -> _Graph:
    g = _Graph()
    for rel in sorted(units):
        for d in units[rel].defs:
            # Later definitions of the same qual in one file (conditional defs)
            # keep the first: deterministic and matches the ast reading order.
            g.nodes.setdefault(d.qual, d)
    for qual, d in g.nodes.items():
        g.defs_by_name.setdefault(qual.split(".")[-1], []).append(qual)
    for name in g.defs_by_name:
        g.defs_by_name[name].sort()

    imports_of, engine = _import_edges(ws, set(units), units)
    g.engines["scoping"] = engine
    engines = {u.engine.split("/")[0] for u in units.values()}
    g.engines["nodes"] = "+".join(sorted(engines)) if engines else "none"

    for rel in sorted(units):
        for caller_qual, callee, line in units[rel].calls:
            targets, tier = _resolve_name(callee, rel, g, imports_of)
            if not targets:
                continue  # not an in-repo definition (builtin, third party)
            g.out_edges.setdefault(caller_qual, []).append((callee, line, targets, tier))
            for t in targets:
                g.in_edges.setdefault(t, []).append((caller_qual, line, tier))

    # Inheritance: a base name resolves through the same ladder.
    for rel in sorted(units):
        for d in units[rel].defs:
            for base in d.bases:
                targets, _tier = _resolve_name(base, rel, g, imports_of)
                for t in targets:
                    if t != d.qual:
                        g.subclasses.setdefault(t, []).append(d.qual)
    for k in g.in_edges:
        g.in_edges[k] = sorted(set(g.in_edges[k]))
    for k in g.subclasses:
        g.subclasses[k] = sorted(set(g.subclasses[k]))
    return g


# ------------------------------------------------------------------- caching
def _unit_key(ws: Workspace, rel: str) -> str:
    """Per-file cache key. The basis is ``stat_fingerprint`` over that ONE
    file, which is what makes an edit cost one re-parse instead of a corpus
    rebuild — the whole point of v2's cache layout."""
    h = hashlib.sha256()
    h.update((_FORMAT + "\n" + rel + "\n").encode("utf-8"))
    stat_fingerprint(ws.root, [rel], h)
    return h.hexdigest()


def _unit_to_json(u: _Unit) -> dict[str, Any]:
    return {
        "rel": u.rel,
        "lang": u.lang,
        "engine": u.engine,
        "defs": [[d.qual, d.rel, d.lineno, d.end, d.kind, d.bases, d.lang] for d in u.defs],
        "calls": [list(c) for c in u.calls],
        "imports": u.imports,
    }


def _unit_from_json(d: dict[str, Any]) -> _Unit:
    u = _Unit(rel=d["rel"], lang=d["lang"], engine=d["engine"])
    u.defs = [_Def(x[0], x[1], x[2], x[3], x[4], list(x[5]), x[6]) for x in d["defs"]]
    u.calls = [(c[0], c[1], int(c[2])) for c in d["calls"]]
    u.imports = list(d.get("imports") or [])
    return u


def _write_json(path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a cache that cannot be written is a slow answer, never an error


def _load_graph(store: Store, ws: Workspace) -> _Graph:
    from ctx.skeleton import language_for

    rels = sorted(
        r
        for r in ws.list_files()[:_MAX_FILES]
        if r.endswith(".py") or language_for(r) is not None
    )
    unit_dir = store.root / "indexes" / "callgraph" / "units"
    keys: dict[str, str] = {rel: _unit_key(ws, rel) for rel in rels}

    # The linked graph is itself cached against the set of unit keys, so an
    # untouched worktree resolves in one read.
    gh = hashlib.sha256()
    gh.update((_FORMAT + "\n").encode("utf-8"))
    for rel in rels:
        gh.update((rel + "\x00" + keys[rel] + "\n").encode("utf-8"))
    graph_path = store.root / "indexes" / "callgraph" / "graphs" / gh.hexdigest()
    if graph_path.is_file():
        try:
            return _graph_from_json(json.loads(graph_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            pass

    units: dict[str, _Unit] = {}
    for rel in rels:
        cache_path = unit_dir / keys[rel]
        unit: _Unit | None = None
        if cache_path.is_file():
            try:
                unit = _unit_from_json(json.loads(cache_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                unit = None
        if unit is None:
            unit = _extract_unit(store, ws, rel)
            if unit is not None:
                _write_json(cache_path, _unit_to_json(unit))
        if unit is not None:
            units[rel] = unit

    g = _link(ws, units)
    _write_json(graph_path, _graph_to_json(g))
    return g


def _graph_to_json(g: _Graph) -> dict[str, Any]:
    return {
        "nodes": {
            q: [d.rel, d.lineno, d.end, d.kind, d.bases, d.lang] for q, d in g.nodes.items()
        },
        "defs_by_name": g.defs_by_name,
        "in_edges": {k: [list(e) for e in v] for k, v in g.in_edges.items()},
        "out_edges": {k: [[a, b, list(c), d] for a, b, c, d in v] for k, v in g.out_edges.items()},
        "subclasses": g.subclasses,
        "engines": g.engines,
    }


def _graph_from_json(d: dict[str, Any]) -> _Graph:
    g = _Graph()
    g.nodes = {
        q: _Def(q, v[0], v[1], v[2], v[3], list(v[4]), v[5]) for q, v in d["nodes"].items()
    }
    g.defs_by_name = {k: list(v) for k, v in d["defs_by_name"].items()}
    g.in_edges = {k: [(e[0], int(e[1]), e[2]) for e in v] for k, v in d["in_edges"].items()}
    g.out_edges = {
        k: [(e[0], int(e[1]), list(e[2]), e[3]) for e in v] for k, v in d["out_edges"].items()
    }
    g.subclasses = {k: list(v) for k, v in d["subclasses"].items()}
    g.engines = dict(d.get("engines") or {})
    return g


# ------------------------------------------------------------------ queries
def _resolve_target(g: _Graph, symbol: str) -> list[str]:
    """Definitions the query names.

    A DOTTED symbol is an exact qualified lookup and stays exact — v2 answers
    it against qual-keyed edges instead of degrading to the bare-name answer.
    A BARE name resolves to every definition with that name: it must never
    short-circuit to a lone module-level qual that happens to equal it, or the
    ambiguity note is suppressed exactly when it is most needed (v1 carried
    this guard; losing it silently narrowed `render` to one of two).
    """
    if "." in symbol:
        if symbol in g.nodes:
            return [symbol]
        # `Class.method` spelled against a nested qual (`Mod.Class.method`).
        hits = sorted(q for q in g.nodes if q.endswith("." + symbol))
        if hits:
            return hits
    return list(g.defs_by_name.get(symbol.split(".")[-1], []))


def _fmt_def(g: _Graph, qual: str) -> str:
    d = g.nodes.get(qual)
    return f"{qual}  {d.rel}:{d.lineno}-{d.end}" if d else qual


def _is_production(rel: str) -> bool:
    return not any(seg in rel for seg in _NON_PRODUCTION)


def _header(g: _Graph, verb: str, symbol: str, extra: str = "") -> list[str]:
    eng = f"nodes {g.engines.get('nodes', '?')} · scoping {g.engines.get('scoping', '?')}"
    tail = f" · {extra}" if extra else ""
    return [f"[ctx {verb} {symbol} · {eng}{tail}]"]


def _ambiguity_note(g: _Graph, symbol: str, targets: list[str]) -> list[str]:
    """SPEC §8: several definitions answer to this name — name them all.

    v1 emitted this on ``callers`` only, and suppressed it whenever a dotted
    query narrowed to one target, which is exactly when the answer it then
    printed was the *unnarrowed* one.
    """
    if len(targets) <= 1:
        return []
    out = [f"  note: {len(targets)} definitions match {symbol!r} (ambiguous) — all are included:"]
    for q in targets[:8]:
        out.append(f"    {_fmt_def(g, q)}")
    if len(targets) > 8:
        out.append(f"    … +{fmt_int(len(targets) - 8)} more")
    return out


def _split_tier(entries: list, tier_index: int, unscoped: bool) -> tuple[list, int]:
    """Scoped rows by default; the unscoped tail declared with a count and the
    flag that resolves it (CONTRIBUTING §4 — declared omission, never silent).

    Returning every repo-wide name match by default is technically honest and
    practically useless: on this repo `callers LogTemplateProfile.detect` had
    one scoped caller and 35 unscoped candidates, and printing all 36 buries
    the answer. The tail is one flag away, and its size is always stated.
    """
    if unscoped:
        return entries, 0
    kept = [e for e in entries if e[tier_index] != _TIER_REPO]
    return kept, len(entries) - len(kept)


def _omission_note(hidden: int, symbol: str, verb: str, noun: str) -> list[str]:
    if not hidden:
        return []
    return [
        f"  omitted: {fmt_int(hidden)} UNSCOPED {noun} (name matched repo-wide; "
        f"the caller's file neither defines nor imports the target)",
        f"    resolve: ctx {verb} {symbol} --unscoped",
    ]


def _rows(g: _Graph, entries: list[tuple[str, int, str]]) -> list[str]:
    """Caller rows, production first, each carrying its call-site line.

    Both groups are labelled whenever both exist: on this repo 26 of 37
    `callers put_blob` rows were tests, sorted alphabetically in among the 11
    production callers that were the actual answer.
    """
    prod = [e for e in entries if _is_production(g.nodes[e[0]].rel if e[0] in g.nodes else e[0])]
    other = [e for e in entries if e not in prod]
    out: list[str] = []
    both = bool(prod) and bool(other)
    for label, group in (("first-party", prod), ("tests/evals", other)):
        if not group:
            continue
        if both:
            out.append(f"  {label} ({fmt_int(len(group))}):")
        for qual, line, tier in group[:_MAX_ROWS]:
            d = g.nodes.get(qual)
            where = f"{d.rel}:{line}" if d else f"?:{line}"
            mark = "" if tier != _TIER_REPO else "  [unscoped]"
            out.append(f"    {qual}  {where}{mark}")
        if len(group) > _MAX_ROWS:
            out.append(f"    … +{fmt_int(len(group) - _MAX_ROWS)} more")
    return out


def cmd_callers(store: Store, ws: Workspace, symbol: str, unscoped: bool = False) -> str:
    g = _load_graph(store, ws)
    targets = _resolve_target(g, symbol)
    out = _header(g, "callers", symbol)
    if not targets:
        out.append(f"  no definition matching {symbol!r} in the workspace")
        return "\n".join(out)
    out += _ambiguity_note(g, symbol, targets)
    entries: list[tuple[str, int, str]] = []
    for t in targets:
        entries.extend(g.in_edges.get(t, []))
    entries = sorted(set(entries))
    entries, hidden = _split_tier(entries, 2, unscoped)
    out.append(f"callers: {fmt_int(len(entries))}")
    out += _rows(g, entries)
    out += _omission_note(hidden, symbol, "callers", "callers")
    out.append("next:")
    out.append(f"  ctx impact {symbol}   ·   ctx def repo:<file>:{symbol.split('.')[-1]}")
    return "\n".join(out)


def cmd_callees(store: Store, ws: Workspace, symbol: str, unscoped: bool = False) -> str:
    g = _load_graph(store, ws)
    targets = _resolve_target(g, symbol)
    out = _header(g, "callees", symbol)
    if not targets:
        out.append(f"  no definition matching {symbol!r} in the workspace")
        return "\n".join(out)
    # v1 unioned the callees of every same-named definition and said nothing:
    # on this repo `callees render` merged 22 unrelated methods into one list.
    out += _ambiguity_note(g, symbol, targets)
    rows: list[tuple[str, int, list[str], str]] = []
    for t in targets:
        rows.extend(g.out_edges.get(t, []))
    rows = sorted(set((a, b, tuple(c), d) for a, b, c, d in rows))
    rows, hidden = _split_tier(rows, 3, unscoped)
    out.append(f"calls (in-repo): {fmt_int(len(rows))}")
    for name, line, quals, tier in rows[:_MAX_ROWS]:
        first = g.nodes.get(quals[0]) if quals else None
        where = f"{first.rel}:{first.lineno}" if first else "?"
        amb = f"  ({fmt_int(len(quals))} candidates)" if len(quals) > 1 else ""
        mark = "  [unscoped]" if tier == _TIER_REPO else ""
        out.append(f"  L{line} → {name}  {where}{amb}{mark}")
    if len(rows) > _MAX_ROWS:
        out.append(f"  … +{fmt_int(len(rows) - _MAX_ROWS)} more call sites")
    out += _omission_note(hidden, symbol, "callees", "call sites")
    return "\n".join(out)


def _reachable(g: _Graph, seeds: list[str], depth: int, unscoped: bool) -> dict[str, int]:
    """Transitive callers by depth. networkx when importable (the same
    optional rung ``repomap`` already ranks with), stdlib BFS otherwise —
    identical results, so the engine is a speed choice, not a semantic one.

    Unscoped edges are excluded by default and, because reachability
    compounds, that is where they do the most damage: v1's frontier walked
    unqualified names, so one collision at depth 1 pulled its whole cone in
    and `impact put_blob` reported 1,902 of 3,313 definitions.
    """

    def edges_of(q: str):
        for caller, _line, tier in g.in_edges.get(q, []):
            if unscoped or tier != _TIER_REPO:
                yield caller

    try:
        import networkx as nx

        dg = nx.DiGraph()
        dg.add_nodes_from(sorted(g.nodes))
        for target in sorted(g.in_edges):
            for caller in edges_of(target):
                # edge points at the caller: reachability = blast radius
                dg.add_edge(target, caller)
        reached: dict[str, int] = {}
        for seed in seeds:
            if seed not in dg:
                continue
            for node, d in nx.single_source_shortest_path_length(dg, seed, cutoff=depth).items():
                if d and (node not in reached or d < reached[node]):
                    reached[node] = d
        return reached
    except Exception:
        pass
    reached = {}
    frontier = set(seeds)
    for d in range(1, depth + 1):
        nxt: set[str] = set()
        for q in frontier:
            for caller in edges_of(q):
                if caller not in reached and caller not in seeds:
                    reached[caller] = d
                    nxt.add(caller)
        frontier = nxt
        if not frontier:
            break
    return reached


def cmd_impact(
    store: Store,
    ws: Workspace,
    symbol: str,
    depth: int = _MAX_DEPTH,
    unscoped: bool = False,
) -> str:
    """Transitive callers (blast radius): everything that reaches ``symbol``."""
    g = _load_graph(store, ws)
    depth = max(1, min(int(depth), _MAX_DEPTH))
    targets = _resolve_target(g, symbol)
    out = _header(g, "impact", symbol, f"transitive callers depth≤{depth}")
    if not targets:
        out.append(f"  no definition matching {symbol!r} in the workspace")
        return "\n".join(out)
    # v1 had no ambiguity note here at all, and its BFS frontier was by
    # unqualified name — which is why a blast radius could reach most of a repo.
    out += _ambiguity_note(g, symbol, targets)
    reached = _reachable(g, targets, depth, unscoped)
    out.append(f"reached (transitive callers): {fmt_int(len(reached))}")
    by_depth: dict[int, list[str]] = {}
    for q, d in reached.items():
        by_depth.setdefault(d, []).append(q)
    for d in sorted(by_depth):
        group = sorted(by_depth[d])
        out.append(f"  depth {d}: {fmt_int(len(group))}")
        for q in group[:_MAX_ROWS]:
            out.append(f"    {_fmt_def(g, q)}")
        if len(group) > _MAX_ROWS:
            out.append(f"    … +{fmt_int(len(group) - _MAX_ROWS)} more at depth {d}")
    if not unscoped:
        wide = _reachable(g, targets, depth, True)
        out += _omission_note(
            len(wide) - len(reached), symbol, "impact", "reachable-via-unscoped nodes"
        )
    out.append("next:")
    out.append(f"  ctx callers {symbol}   (direct only)")
    return "\n".join(out)


def cmd_impls(store: Store, ws: Workspace, symbol: str, depth: int = _MAX_DEPTH) -> str:
    """Type hierarchy: what implements or extends this type.

    The question ``ctx q 'refs Profile | group file'`` could only approximate —
    it returned import lines and ``class X(Profile):`` declarations mixed with
    test files, for the reader to sort out. Bases are resolved through the same
    tier ladder as call sites, so an inherited name is scoped, not grepped.
    """
    g = _load_graph(store, ws)
    depth = max(1, min(int(depth), _MAX_DEPTH))
    targets = _resolve_target(g, symbol)
    out = _header(g, "impls", symbol, f"subtypes depth≤{depth}")
    if not targets:
        out.append(f"  no definition matching {symbol!r} in the workspace")
        return "\n".join(out)
    out += _ambiguity_note(g, symbol, targets)
    classes = [t for t in targets if g.nodes[t].kind in ("class", "struct", "trait", "interface")]
    if not classes:
        out.append(f"  {symbol!r} is not a type ({g.nodes[targets[0]].kind}); nothing extends it")
        return "\n".join(out)

    seen: dict[str, int] = {}
    frontier = list(classes)
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for q in frontier:
            for sub in g.subclasses.get(q, []):
                if sub not in seen and sub not in classes:
                    seen[sub] = d
                    nxt.append(sub)
        frontier = nxt
        if not frontier:
            break
    out.append(f"subtypes: {fmt_int(len(seen))}")
    by_depth: dict[int, list[str]] = {}
    for q, d in seen.items():
        by_depth.setdefault(d, []).append(q)
    for d in sorted(by_depth):
        group = sorted(by_depth[d])
        label = "direct" if d == 1 else f"depth {d}"
        out.append(f"  {label}: {fmt_int(len(group))}")
        for q in group[:_MAX_ROWS]:
            out.append(f"    {_fmt_def(g, q)}")
        if len(group) > _MAX_ROWS:
            out.append(f"    … +{fmt_int(len(group) - _MAX_ROWS)} more at {label}")
    # The inverse direction, when the queried type itself extends something.
    bases = sorted({b for t in classes for b in g.nodes[t].bases})
    if bases:
        out.append(f"  extends: {', '.join(bases)}")
    out.append("next:")
    out.append(f"  ctx callers {symbol}   ·   ctx def repo:<file>:{symbol.split('.')[-1]}")
    return "\n".join(out)
