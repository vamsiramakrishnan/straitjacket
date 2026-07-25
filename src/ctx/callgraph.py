"""Deterministic call graph over the workspace's Python sources.

`ctx callers`/`callees`/`impact` answer the questions that turn a multi-turn
grep-and-read trace into one query: who calls this, what does it call, and
what is transitively reachable (blast radius). Nodes we already had via
``ctx def``/``refs``; this module adds the *edges*.

Doctrine (README §dependency): pure stdlib ``ast`` — no tree-sitter, no
native binary, no daemon. Edge resolution is NAME-BASED (a call to ``foo``
binds to any in-repo ``def foo``): approximate but disclosed, exactly like
the ctags map engine. It is deterministic (a pure function of worktree
bytes) and worktree-hash cached, so the graph is always current without a
background watcher — the model tokensave removed twice for runaway resource
use. Cross-file resolution is by unqualified name; ambiguous names resolve
to every candidate and the ambiguity is reported, never hidden (SPEC §8).
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field

from ctx.store import Store
from ctx.textutil import fmt_int
from ctx.workspace import Workspace, stat_fingerprint

_FORMAT = "ctx.callgraph/v1"
_MAX_FILES = 5000
_MAX_DEPTH = 6  # hard bound on impact/reachability recursion


@dataclass
class _Node:
    qual: str  # ClassName.method or function
    rel: str
    lineno: int
    end: int


@dataclass
class _Graph:
    # unqualified name -> list of qualified definition nodes with that name
    defs_by_name: dict[str, list[_Node]] = field(default_factory=dict)
    nodes: dict[str, _Node] = field(default_factory=dict)  # qual -> node
    # qualified caller -> set of unqualified callee names (call-site order kept
    # via a list deduped on first sight for determinism)
    out_edges: dict[str, list[str]] = field(default_factory=dict)
    # unqualified callee name -> sorted list of qualified callers
    in_edges: dict[str, list[str]] = field(default_factory=dict)


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, g: _Graph) -> None:
        self.rel = rel
        self.g = g
        self.stack: list[str] = []

    def _qual(self) -> str:
        return ".".join(self.stack)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
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
        n = _Node(qual=qual, rel=self.rel, lineno=node.lineno, end=end)
        self.g.nodes[qual] = n
        self.g.defs_by_name.setdefault(node.name, []).append(n)
        # Collect calls lexically inside this function (not nested defs — those
        # get their own node and their own edges).
        callees: list[str] = []
        seen: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node:
                continue
            if isinstance(child, ast.Call):
                name = _callee_name(child.func)
                if name and name not in seen:
                    seen.add(name)
                    callees.append(name)
        if callees:
            self.g.out_edges[qual] = callees
        # descend for nested defs / classes
        self.generic_visit(node)
        self.stack.pop()


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr  # method/attr tail: self.foo() -> "foo"
    return None


def _build(ws: Workspace) -> _Graph:
    g = _Graph()
    rels = [r for r in ws.list_files()[:_MAX_FILES] if r.endswith(".py")]
    for rel in sorted(rels):
        try:
            source = (ws.root / rel).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue
        _Visitor(rel, g).visit(tree)
    # Invert edges to in_edges, resolving only names that name an in-repo def.
    known = set(g.defs_by_name)
    inv: dict[str, set[str]] = {}
    for caller, callees in g.out_edges.items():
        for name in callees:
            if name in known:
                inv.setdefault(name, set()).add(caller)
    g.in_edges = {name: sorted(callers) for name, callers in inv.items()}
    return g


def _graph_cache_key(ws: Workspace, rels: list[str]) -> str:
    """Key for the serialized call graph. Invalidation basis: the format
    version plus ``ctx.workspace.stat_fingerprint`` over the Python files
    that were parsed — the same one basis ``repomap`` uses."""
    h = hashlib.sha256()
    h.update((_FORMAT + "\n").encode("utf-8"))
    stat_fingerprint(ws.root, rels, h)
    return h.hexdigest()


def _load_graph(store: Store, ws: Workspace) -> _Graph:
    rels = sorted(r for r in ws.list_files()[:_MAX_FILES] if r.endswith(".py"))
    key = _graph_cache_key(ws, rels)
    cache_path = store.root / "indexes" / "callgraph" / key
    if cache_path.is_file():
        try:
            return _from_json(json.loads(cache_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            pass
    g = _build(ws)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_to_json(g), sort_keys=True), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError:
        pass
    return g


def _to_json(g: _Graph) -> dict:
    return {
        "nodes": {q: [n.rel, n.lineno, n.end] for q, n in g.nodes.items()},
        "defs_by_name": {k: [n.qual for n in v] for k, v in g.defs_by_name.items()},
        "out_edges": g.out_edges,
        "in_edges": g.in_edges,
    }


def _from_json(d: dict) -> _Graph:
    g = _Graph()
    g.nodes = {q: _Node(q, v[0], v[1], v[2]) for q, v in d["nodes"].items()}
    g.defs_by_name = {
        k: [g.nodes[q] for q in quals if q in g.nodes]
        for k, quals in d["defs_by_name"].items()
    }
    g.out_edges = {k: list(v) for k, v in d["out_edges"].items()}
    g.in_edges = {k: list(v) for k, v in d["in_edges"].items()}
    return g


# ------------------------------------------------------------------ queries
def _resolve(g: _Graph, symbol: str) -> tuple[str, list[_Node]]:
    """Return (unqualified_name, matching def nodes). Accepts a dotted qual
    (Class.method) or a bare name."""
    name = symbol.split(".")[-1]
    # A DOTTED symbol is an exact qualified lookup. A BARE name resolves to
    # every definition with that name (ambiguity is surfaced, not hidden) —
    # never short-circuit to a lone module-level qual that happens to match.
    if "." in symbol and symbol in g.nodes:
        return name, [g.nodes[symbol]]
    return name, list(g.defs_by_name.get(name, []))


def _fmt_node(n: _Node) -> str:
    return f"{n.qual}  {n.rel}:{n.lineno}-{n.end}"


def cmd_callers(store: Store, ws: Workspace, symbol: str) -> str:
    g = _load_graph(store, ws)
    name, targets = _resolve(g, symbol)
    out = [f"[ctx callers {symbol} · engine ast · name-resolved]"]
    if not targets:
        out.append(f"  no definition named {name!r} in workspace Python sources")
        return "\n".join(out)
    if len(targets) > 1:
        out.append(f"  note: {len(targets)} definitions named {name!r} (ambiguous):")
        for n in targets[:8]:
            out.append(f"    {_fmt_node(n)}")
    callers = g.in_edges.get(name, [])
    out.append(f"callers (exact by name): {fmt_int(len(callers))}")
    for q in callers:
        n = g.nodes.get(q)
        out.append(f"  {_fmt_node(n)}" if n else f"  {q}")
    out.append("next:")
    out.append(f"  ctx impact {name}   ·   ctx def repo:<file>:{name}")
    return "\n".join(out)


def cmd_callees(store: Store, ws: Workspace, symbol: str) -> str:
    g = _load_graph(store, ws)
    name, targets = _resolve(g, symbol)
    out = [f"[ctx callees {symbol} · engine ast · name-resolved]"]
    if not targets:
        out.append(f"  no definition named {name!r} in workspace Python sources")
        return "\n".join(out)
    quals = [n.qual for n in targets]
    calls: list[str] = []
    seen: set[str] = set()
    for q in quals:
        for callee in g.out_edges.get(q, []):
            if callee in g.defs_by_name and callee not in seen:
                seen.add(callee)
                calls.append(callee)
    out.append(f"calls (in-repo, by name): {fmt_int(len(calls))}")
    for c in sorted(calls):
        nodes = g.defs_by_name.get(c, [])
        loc = f" {nodes[0].rel}:{nodes[0].lineno}" if len(nodes) == 1 else f" ({len(nodes)} defs)"
        out.append(f"  {c}{loc}")
    return "\n".join(out)


def cmd_impact(store: Store, ws: Workspace, symbol: str, depth: int = _MAX_DEPTH) -> str:
    """Transitive callers (blast radius): everything that reaches ``symbol``."""
    g = _load_graph(store, ws)
    name, targets = _resolve(g, symbol)
    depth = max(1, min(int(depth), _MAX_DEPTH))
    out = [f"[ctx impact {symbol} · engine ast · transitive callers depth≤{depth}]"]
    if not targets:
        out.append(f"  no definition named {name!r} in workspace Python sources")
        return "\n".join(out)
    # BFS over in_edges. Frontier is by unqualified name; each caller qual maps
    # back to its own name for the next hop.
    reached: dict[str, int] = {}  # qual -> depth first seen
    frontier = {name}
    for d in range(1, depth + 1):
        nxt: set[str] = set()
        for nm in frontier:
            for caller_qual in g.in_edges.get(nm, []):
                if caller_qual not in reached:
                    reached[caller_qual] = d
                    nxt.add(caller_qual.split(".")[-1])
        frontier = nxt
        if not frontier:
            break
    out.append(f"reached (transitive callers): {fmt_int(len(reached))}")
    by_depth: dict[int, list[str]] = {}
    for q, d in reached.items():
        by_depth.setdefault(d, []).append(q)
    for d in sorted(by_depth):
        out.append(f"  depth {d}: {fmt_int(len(by_depth[d]))}")
        for q in sorted(by_depth[d])[:20]:
            n = g.nodes.get(q)
            out.append(f"    {_fmt_node(n)}" if n else f"    {q}")
        if len(by_depth[d]) > 20:
            out.append(f"    … +{fmt_int(len(by_depth[d]) - 20)} more at depth {d}")
    out.append("next:")
    out.append(f"  ctx callers {name}   (direct only)")
    return "\n".join(out)
