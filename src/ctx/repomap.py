"""Ranked repository map (ROADMAP M-C): global awareness at constant cost.

Files are ranked by a deterministic reference-graph score (imports, damped
PageRank, alphabetical tie-breaks), boosted by recent run evidence held in
the store, fitted greedily to a token budget, and cached under a
worktree-content key. Every emitted symbol line is addressable via the
existing ``ctx get repo:<file> --symbol <name>`` selector.

Engine policy: when the optional deps grimp (import-graph resolution) and
networkx (converging PageRank) are importable — and ``CTX_MAP_ENGINE`` is
not ``builtin`` — they are used and disclosed in the header as
``engine grimp+networkx``; any engine failure at build time degrades
silently to the builtin resolver/ranker (``engine builtin``), which is
always available.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ctx.store import Store, _atomic_write
from ctx.textutil import fmt_int
from ctx.workspace import Workspace

_FORMAT = "ctx.map/v3"  # v3: priced survivors (~tok · defs on each entry)
_ENGINE_BUILTIN = "builtin"
_ENGINE_GRIMP = "grimp+networkx"
_DAMPING = 0.85
_ITERATIONS = 10
_EVIDENCE_RUNS = 5  # most recent run manifests scanned
_EVIDENCE_BYTES = 262144  # per-stream scan bound
_EVIDENCE_BOOST = 3.0
_SYMBOLS_PER_FILE = 5
_HOT_SHOWN = 8
_MAX_FILES = 5000


@dataclass
class _FileMap:
    rel: str
    symbols: list[tuple[str, str]] = field(default_factory=list)  # (name, sig)
    imported_by: int = 0
    score: float = 1.0
    focused: bool = False
    size: int = 0  # bytes — the price tag on survivors (PRICED-CONTEXT M3)


# ------------------------------------------------------------- python (ast)
def _fmt_args(a: ast.arguments) -> str:
    parts = [x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    parts += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return ", ".join(parts)


def _py_symbols(tree: ast.Module) -> list[tuple[str, str]]:
    """Top-level defs/classes in source order with ast-derived signatures."""
    syms: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            syms.append((node.name, f"({_fmt_args(node.args)})"))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            syms.append((node.name, f"class({bases})" if bases else "class"))
    return syms


def _module_index(py_files: list[str]) -> dict[str, list[str]]:
    """Dotted-suffix module index: 'src/ctx/store.py' answers to
    'src.ctx.store', 'ctx.store', and 'store' (shortest-suffix heuristics;
    ambiguity resolves to the alphabetically first candidate)."""
    index: dict[str, list[str]] = {}
    for rel in py_files:
        parts = rel[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        for i in range(len(parts)):
            index.setdefault(".".join(parts[i:]), []).append(rel)
    for candidates in index.values():
        candidates.sort()
    return index


def _resolve_module(dotted: str, index: dict[str, list[str]]) -> str | None:
    parts = dotted.split(".")
    while parts:
        hit = index.get(".".join(parts))
        if hit:
            return hit[0]
        parts = parts[:-1]
    return None


def _py_imports(tree: ast.Module, rel: str, index: dict[str, list[str]]) -> set[str]:
    """Repo files imported by ``rel``; unresolvable imports are ignored."""
    pkg = rel.split("/")[:-1]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_module(alias.name, index)
                if target and target != rel:
                    out.add(target)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                drop = node.level - 1
                if drop > len(pkg):
                    continue
                base = pkg[: len(pkg) - drop] if drop else list(pkg)
                mod = base + (node.module.split(".") if node.module else [])
                candidates = [".".join(mod + [a.name]) for a in node.names]
            else:
                mod_name = node.module or ""
                candidates = [f"{mod_name}.{a.name}" for a in node.names]
            for cand in candidates:
                target = _resolve_module(cand, index)
                if target and target != rel:
                    out.add(target)
    return out


# ---------------------------------------------------------- ctags (optional)
def _ctags_enabled() -> bool:
    if os.environ.get("CTX_NO_CTAGS"):
        return False
    return shutil.which("ctags") is not None


def _ctags_symbols(ws: Workspace, rels: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Opportunistic universal-ctags pass over non-Python files; any failure
    degrades transparently to omitting them."""
    if not rels or not _ctags_enabled():
        return {}
    import json

    try:
        proc = subprocess.run(
            ["ctags", "--output-format=json", "--fields=+n", "--sort=no", "-f", "-", *rels[:500]],
            cwd=ws.root,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    tagged: dict[str, list[tuple[int, str, str]]] = {}
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            tag = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") != "tag" or not tag.get("name") or not tag.get("path"):
            continue
        path = str(tag["path"]).replace("\\", "/").removeprefix("./")
        tagged.setdefault(path, []).append(
            (int(tag.get("line") or 0), str(tag["name"]), str(tag.get("kind") or "symbol"))
        )
    out: dict[str, list[tuple[str, str]]] = {}
    for path, tags in tagged.items():
        tags.sort()
        seen: set[str] = set()
        syms: list[tuple[str, str]] = []
        for _, name, kind in tags:
            if name not in seen:
                seen.add(name)
                syms.append((name, kind))
        out[path] = syms
    return out


# ------------------------------------------------------- evidence weighting
def _recent_run_ids(store: Store) -> list[str]:
    try:
        rows = store.db.execute(
            "SELECT id FROM objects WHERE kind='run' ORDER BY created_at DESC, id LIMIT ?",
            (_EVIDENCE_RUNS,),
        ).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def _hot_paths(store: Store, run_ids: list[str], rels: list[str]) -> list[str]:
    """Repo-relative paths mentioned in the captured streams of the most
    recent run manifests. Deterministic given store state; empty when the
    store holds no runs."""
    hot: set[str] = set()
    for mid in run_ids:
        try:
            manifest = store.get_manifest(mid)
        except Exception:
            continue
        for name in ("stdout", "stderr"):
            meta = (manifest.get("streams") or {}).get(name)
            if not meta or not meta.get("bytes"):
                continue
            try:
                data = store.get_blob(str(meta["blob"]).removeprefix("sha256:"))
            except Exception:
                continue
            text = data[:_EVIDENCE_BYTES].decode("utf-8", "replace")
            for rel in rels:
                if rel not in hot and rel in text:
                    hot.add(rel)
    return sorted(hot)


# ------------------------------------------- engine selection (grimp+networkx)
def _select_engine() -> str:
    """``grimp+networkx`` when both optional deps import and CTX_MAP_ENGINE
    does not force the builtin path; ``builtin`` otherwise."""
    if os.environ.get("CTX_MAP_ENGINE") == _ENGINE_BUILTIN:
        return _ENGINE_BUILTIN
    try:
        import grimp  # noqa: F401
        import networkx  # noqa: F401
    except Exception:
        return _ENGINE_BUILTIN
    return _ENGINE_GRIMP


def _grimp_top_levels(ws: Workspace) -> list[tuple[str, Path]]:
    """Top-level import targets discovered from the workspace: directories
    containing ``__init__.py`` (plus single-module ``*.py`` top-levels) under
    the root and, for src-layouts, under ``root/src``. Sorted (name, base)."""
    bases = [ws.root]
    src = ws.root / "src"
    if src.is_dir():
        bases.append(src)
    found: dict[str, Path] = {}
    for base in bases:
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name
            if child.is_dir() and (child / "__init__.py").is_file() and name.isidentifier():
                found.setdefault(name, base)
            elif child.is_file() and name.endswith(".py"):
                stem = name[: -len(".py")]
                if stem.isidentifier() and stem not in ("__init__", "__main__"):
                    found.setdefault(stem, base)
    return sorted(found.items())


def _grimp_edges(ws: Workspace, rels: set[str]) -> dict[str, set[str]]:
    """File-level import edges resolved by grimp (namespace packages,
    relative imports, src-layouts). The workspace root (and ``src/``) is
    placed on sys.path for the build, per grimp's importable-package API.
    Raises on any engine trouble; the caller falls back to the builtin path."""
    import importlib
    import sys

    import grimp

    tops = _grimp_top_levels(ws)
    if not tops:
        raise RuntimeError("no importable top-level packages discovered")
    search = [str(b) for b in dict.fromkeys(base for _, base in tops)]
    saved = list(sys.path)
    sys.path[:0] = search
    importlib.invalidate_caches()
    try:
        graph = grimp.build_graph(*[name for name, _ in tops], cache_dir=None)
    finally:
        sys.path[:] = saved
        importlib.invalidate_caches()

    base_for = dict(tops)

    def to_rel(module: str) -> str | None:
        parts = module.split(".")
        base = base_for.get(parts[0])
        if base is None:
            return None
        stem = base.joinpath(*parts)
        for cand in (stem.with_suffix(".py"), stem / "__init__.py"):
            if cand.is_file():
                try:
                    return cand.relative_to(ws.root).as_posix()
                except ValueError:
                    return None
        return None

    mod_rel: dict[str, str] = {}
    for module in sorted(graph.modules):
        rel = to_rel(module)
        if rel is not None and rel in rels:
            mod_rel[module] = rel

    edges: dict[str, set[str]] = {}
    for module in sorted(mod_rel):
        rel = mod_rel[module]
        for imported in sorted(graph.find_modules_directly_imported_by(module)):
            target = mod_rel.get(imported)
            if target and target != rel:
                edges.setdefault(rel, set()).add(target)
    return edges


def _rank_networkx(files: dict[str, _FileMap], edges: dict[str, set[str]]) -> None:
    """networkx PageRank (alpha=0.85) over a DiGraph built in sorted order.
    Scores are rounded to 8 decimals before ranking so float-ordering noise
    can never perturb the alphabetical tie-breaks."""
    import networkx as nx

    names = sorted(files)
    g = nx.DiGraph()
    g.add_nodes_from(names)
    for importer in sorted(edges):
        if importer not in files:
            continue
        for target in sorted(edges[importer]):
            if target in files:
                g.add_edge(importer, target)
    try:
        score = nx.pagerank(g, alpha=_DAMPING)
    except ImportError:
        # networkx>=3 backs nx.pagerank with scipy; without scipy/numpy use
        # its pure-python power iteration (same algorithm and convergence).
        from networkx.algorithms.link_analysis.pagerank_alg import _pagerank_python

        score = _pagerank_python(g, alpha=_DAMPING)
    for n in names:
        files[n].score = round(float(score.get(n, 0.0)), 8)
        files[n].imported_by = g.in_degree(n)


# ------------------------------------------------------------------ ranking
def _rank(files: dict[str, _FileMap], edges: dict[str, set[str]]) -> None:
    """Fixed-iteration damped reference-count ranking (PageRank-style).
    Deterministic: sorted iteration order, alphabetical tie-breaks later."""
    names = sorted(files)
    incoming: dict[str, list[str]] = {n: [] for n in names}
    outdeg: dict[str, int] = {}
    for importer, targets in edges.items():
        outdeg[importer] = len(targets)
        for t in sorted(targets):
            incoming[t].append(importer)
    score = {n: 1.0 for n in names}
    for _ in range(_ITERATIONS):
        score = {
            n: (1.0 - _DAMPING)
            + _DAMPING * sum(score[imp] / outdeg[imp] for imp in incoming[n])
            for n in names
        }
    for n in names:
        files[n].score = score[n]
        files[n].imported_by = len(incoming[n])


# ---------------------------------------------------------------- rendering
def _render(
    files: list[_FileMap], hot: list[str], budget: int, n_files: int, engine: str
) -> str:
    budget_bytes = max(budget, 1) * 4
    tail_reserve = 48
    lines: list[str] = [
        f"[ctx map {fmt_int(n_files)} files · budget {budget} tok · engine {engine}]"
    ]
    used = len(lines[0].encode("utf-8")) + 1 + tail_reserve

    hot_in_map = [h for h in hot if any(f.rel == h for f in files)]
    if hot_in_map:
        section = ["hot (recent run evidence):"] + [f"  {h}" for h in hot_in_map[:_HOT_SHOWN]]
        for line in section:
            cost = len(line.encode("utf-8")) + 1
            if used + cost > budget_bytes:
                break
            lines.append(line)
            used += cost

    omitted_files = 0
    omitted_symbols = 0
    exhausted = False
    for fm in files:
        header = f"repo:{fm.rel}"
        if fm.size:
            from ctx.textutil import fmt_tokens_coarse

            header += f" · {fmt_tokens_coarse(max(1, fm.size // 4))} tok · {len(fm.symbols)}d"
        if fm.imported_by:
            header += f" · imported-by {fm.imported_by}"
        cost = len(header.encode("utf-8")) + 1
        if exhausted or used + cost > budget_bytes:
            exhausted = True
            omitted_files += 1
            omitted_symbols += len(fm.symbols)
            continue
        lines.append(header)
        used += cost
        shown = 0
        for name, sig in fm.symbols[:_SYMBOLS_PER_FILE]:
            sym_line = f"  repo:{fm.rel} --symbol {name} · {sig}"
            cost = len(sym_line.encode("utf-8")) + 1
            if used + cost > budget_bytes:
                break
            lines.append(sym_line)
            used += cost
            shown += 1
        omitted_symbols += len(fm.symbols) - shown

    lines.append(f"omitted: {fmt_int(omitted_files)} files · {fmt_int(omitted_symbols)} symbols")
    return "\n".join(lines)


# -------------------------------------------------------------------- cache
def _cache_key(
    ws: Workspace,
    rels: list[str],
    budget: int,
    focus: str,
    run_ids: list[str],
    ctags: bool,
    engine: str,
) -> str:
    h = hashlib.sha256()
    h.update(
        f"{_FORMAT}\nbudget={budget}\nfocus={focus}\nctags={int(ctags)}\n"
        f"engine={engine}\n".encode("utf-8")
    )
    for mid in run_ids:
        h.update(f"run={mid}\n".encode("utf-8"))
    for rel in rels:
        try:
            st = (ws.root / rel).stat()
        except OSError:
            continue
        h.update(f"{rel}|{st.st_mtime_ns}|{st.st_size}\n".encode("utf-8"))
    return h.hexdigest()


# ----------------------------------------------------------------- assembly
def repo_map(
    store: Store, ws: Workspace, *, budget: int = 600, focus: str | None = None
) -> str:
    """Deterministic, budget-fitted map of the workspace. Byte-identical for
    identical worktree + store state (worktree-keyed cache)."""
    focus_norm = (focus or "").strip().lower()
    rels = ws.list_files()[:_MAX_FILES]
    run_ids = _recent_run_ids(store)
    ctags = _ctags_enabled()
    engine = _select_engine()

    key = _cache_key(ws, rels, budget, focus_norm, run_ids, ctags, engine)
    cache_path = store.root / "indexes" / "maps" / key
    if cache_path.is_file():
        try:
            return cache_path.read_text(encoding="utf-8")
        except OSError:
            pass

    py_files = [r for r in rels if r.endswith(".py")]
    index = _module_index(py_files)

    files: dict[str, _FileMap] = {}
    edges: dict[str, set[str]] = {}
    for rel in py_files:
        try:
            source = (ws.root / rel).read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        fm = _FileMap(rel=rel, size=len(source.encode("utf-8")))
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            fm.symbols = _py_symbols(tree)
            edges[rel] = _py_imports(tree, rel, index)
        files[rel] = fm

    for rel, syms in sorted(_ctags_symbols(ws, [r for r in rels if not r.endswith(".py")]).items()):
        if rel not in files and syms:
            try:
                size = (ws.root / rel).stat().st_size
            except OSError:
                size = 0
            files[rel] = _FileMap(rel=rel, symbols=list(syms), size=size)

    if engine == _ENGINE_GRIMP:
        try:
            _rank_networkx(files, _grimp_edges(ws, set(files)))
        except Exception:
            engine = _ENGINE_BUILTIN  # silent fallback; header discloses it
            _rank(files, edges)
    else:
        _rank(files, edges)

    hot = _hot_paths(store, run_ids, sorted(files))
    for rel in hot:
        if rel in files:
            files[rel].score *= _EVIDENCE_BOOST
    if focus_norm:
        for fm in files.values():
            fm.focused = focus_norm in fm.rel.lower() or any(
                focus_norm in name.lower() for name, _ in fm.symbols
            )

    ranked = sorted(files.values(), key=lambda f: (not f.focused, -f.score, f.rel))
    rendered = _render(ranked, hot, budget, len(ranked), engine)

    try:
        _atomic_write(cache_path, rendered.encode("utf-8"))
    except OSError:
        pass
    return rendered
