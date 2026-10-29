"""Tree-sitter skeleton tier (docs/ALGEBRA.md M-F): imports, types, and
signatures with line ranges for code files, house-styled after Maki.

Skeletons are **derived artifacts**: canonical-JSON blobs keyed by the
source file's blob hash *and* the set of backends able to run (parse once
per content per capability set). Each symbol row carries name, kind,
signature, line range, enclosing scope, and a minted span so bodies stay
retrievable without re-emission.

Backend chain (absence degrades, never errors — the jedi/ripgrep pattern):

    tree-sitter (optional ``[code]`` extra) → universal-ctags on PATH
        → stdlib ast (python only) → none

``CTX_NO_CTAGS`` removes the ctags rung exactly as an absent binary does
(the same variable also silences the map's ctags pass in ``repomap``).

The frozen schema (``ctx.skeleton/v1``) is the seam the fact store (M-G)
builds against — treat it as an interchange format, not an implementation
detail. Absolute paths never enter skeleton bytes or rendered outlines;
only repo-relative POSIX paths are stored.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ctx.execution import snapshot_file
from ctx.store import Store, canonical_json
from ctx.textutil import estimate_tokens
from ctx.workspace import Workspace

SKELETON_SCHEMA = "ctx.skeleton/v1"

# Longest signature line retained per symbol (minified-line guard).
_SIG_MAX = 200


class BackendUnavailable(Exception):
    """A skeleton backend cannot run here (missing import / binary)."""


# Code languages only — prose/config formats never get a skeleton and fall
# back to the generic stats path. Extension map is deterministic on the
# repo-relative path, never on content sniffing.
_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c",
    ".cpp": "c++", ".cc": "c++", ".hpp": "c++", ".hh": "c++",
    ".cs": "c#", ".swift": "swift", ".scala": "scala", ".lua": "lua",
    ".sh": "shell", ".bash": "shell",
}


def language_for(rel_path: str) -> str | None:
    """Skeleton language for a repo-relative path, or None (no skeleton)."""
    return _LANG_BY_EXT.get(Path(rel_path).suffix.lower())


# --------------------------------------------------------------------------
# derived-blob cache (in-catalog manifest, keyed by source blob hash + path)
# --------------------------------------------------------------------------
def _backend_fingerprint(language: str | None) -> str:
    """Which rungs of the backend chain can actually run here, right now.

    Content-addressing a skeleton assumes the same bytes give the same parse.
    Across environments that is false: with no ctags on PATH a C file yields
    zero symbols, and with ctags it yields a hundred. Keying on content alone
    caches whichever answer came first and serves it forever, so installing
    the missing dependency changes nothing — and a stale hit is
    indistinguishable from a correct one, which is what makes it expensive.
    The capability set is therefore part of skeleton identity.

    Probed through the same seams ``_extract`` uses, so this can never claim
    a rung the chain would not actually run; and per-language, so adding a Go
    grammar does not invalidate every Python skeleton already computed.
    """
    if language is None:
        return "none"
    rungs: list[str] = []
    try:
        _tree_sitter_extract("", language)
        rungs.append("ts")
    except Exception:
        pass
    if _ctags_path():
        rungs.append("ctags")
    if language == "python":
        rungs.append("ast")
    return "+".join(rungs) or "none"


def _skeleton_cache_key(source_hash: str, rel: str, backends: str) -> str:
    """Deterministic catalog id for the skeleton of (source bytes, path,
    backends).

    The key manifest is a pure function of the source blob hash, the
    repo-relative path (part of skeleton identity because the frozen schema
    stores ``file``), and the backend fingerprint; lookup is a primary-key
    catalog query.
    """
    body = {
        "backends": backends,
        "file": rel,
        "schema": SKELETON_SCHEMA,
        "source": f"sha256:{source_hash}",
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _cached_skeleton(store: Store, key: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT meta FROM objects WHERE id=? AND kind='skeleton'", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        meta = json.loads(row[0])
        skel_hash = str(meta.get("skeleton", "")).removeprefix("sha256:")
        if not skel_hash:
            return None
        return json.loads(store.get_blob(skel_hash).decode("utf-8"))
    except Exception:
        return None  # lost blob (GC) or corrupt row: recompute, re-register


# --------------------------------------------------------------------------
# public API (frozen for the fact-store tier)
# --------------------------------------------------------------------------
def skeleton_for(store: Store, ws: Workspace, rel_path: str) -> dict[str, Any]:
    """Compute (or return cached) the ``ctx.skeleton/v1`` for one file.

    Snapshot-on-read: the skeleton describes the snapshotted bytes, keyed
    by their blob hash — same source bytes ⇒ same skeleton bytes ⇒ zero
    recompute. The returned dict is always the JSON round-trip of the
    stored canonical bytes, so compute and cache paths are byte-equivalent.
    """
    snap = snapshot_file(store, ws, rel_path)
    rel = str(snap["path"])
    src_hash = str(snap["blob"]).removeprefix("sha256:")
    language = language_for(rel)
    key = _skeleton_cache_key(src_hash, rel, _backend_fingerprint(language))
    cached = _cached_skeleton(store, key)
    if cached is not None:
        return cached

    source = store.get_blob(src_hash).decode("utf-8", "replace")
    symbols, imports, parser = _extract(source, language, rel)

    n_lines = len(source.splitlines())
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        a, b = sym["range"]
        if n_lines:
            a = max(1, min(int(a), n_lines))
            b = max(a, min(int(b), n_lines))
        else:
            a, b = 1, 1
        rows.append({**sym, "range": [a, b]})
    rows.sort(key=lambda s: (s["range"][0], s["range"][1], s["name"], s["kind"]))
    for sym in rows:
        sym["span"] = store.register_span(
            src_hash, "region", a=sym["range"][0], b=sym["range"][1]
        )

    skeleton = {
        "schema": SKELETON_SCHEMA,
        "file": rel,
        "blob": f"sha256:{src_hash}",
        "language": language,
        "parser": parser,
        "symbols": rows,
        "imports": sorted(dict.fromkeys(imports)),
    }
    skel_bytes = canonical_json(skeleton)
    skel_hash = store.put_blob(skel_bytes)
    # In-catalog manifest: kind="skeleton", id = deterministic key above —
    # the source-hash → skeleton-blob mapping IS the cache index.
    store._register(
        key,
        "skeleton",
        {
            "schema": SKELETON_SCHEMA,
            "file": rel,
            "source": f"sha256:{src_hash}",
            "skeleton": f"sha256:{skel_hash}",
        },
    )
    return json.loads(skel_bytes.decode("utf-8"))


def backend_roster() -> dict[str, str]:
    """Which backend chain is live for each language, here.

    Exists because the degradation is otherwise silent. With no universal-ctags
    on PATH, `ctx map` prints "0 files" for a Go repository and no verb says
    why; `ctx doctor` renders this so a stripped install is visible *before*
    somebody measures against it and reports the environment as a finding.
    """
    return {
        lang: _backend_fingerprint(lang)
        for lang in sorted(set(_LANG_BY_EXT.values()))
    }


def skeleton_outline(skeleton: dict[str, Any], budget_tokens: int) -> str:
    """Priced outline rendering of a skeleton: census-before-detail.

    Every symbol's identity (name · kind · range, plus signature when the
    budget allows) is the REQUIRED census; bodies stay retrievable via the
    minted spans. Under a hard budget the ladder degrades hierarchically —
    signatures drop first, then rows compact to a declared omission line
    carrying group-by-kind counts (census-of-census). Never silent.
    """
    file = str(skeleton.get("file", ""))
    symbols = list(skeleton.get("symbols") or [])
    imports = list(skeleton.get("imports") or [])
    head = [
        f"[ctx skeleton repo:{file}]",
        f"language: {skeleton.get('language') or 'unknown'} · parser {skeleton.get('parser')}"
        f" · {len(symbols)} symbols · {len(imports)} imports",
    ]
    tail = []
    if imports:
        tail.append("imports: " + " · ".join(str(i) for i in imports))
    tail.append("next:")
    tail.append(f"  ctx get repo:{file} --lines A:B")

    def _row(sym: dict[str, Any], with_sig: bool) -> str:
        a, b = sym["range"]
        indent = "    " if sym.get("scope") else "  "
        parts = [f"{indent}{sym['kind']} {sym['name']} L{a}-{b}"]
        if sym.get("span"):
            parts.append(f"span {sym['span']}")
        if with_sig and sym.get("signature"):
            parts.append(str(sym["signature"]))
        return " · ".join(parts)

    def _render(body: list[str]) -> str:
        return "\n".join(head + ["outline (census):"] + body + tail)

    def _fits(text: str) -> bool:
        return estimate_tokens(len(text.encode("utf-8"))) <= budget_tokens

    if not symbols:
        return "\n".join(head + ["outline (census): no symbols found"] + tail)

    # Rung 1: full census with signatures.
    full = _render([_row(s, True) for s in symbols])
    if _fits(full):
        return full
    # Rung 2: identities only (signatures omitted, declared).
    bare_rows = [_row(s, False) for s in symbols]
    bare = _render(["  (signatures omitted: budget)"] + bare_rows)
    if _fits(bare):
        return bare
    # Rung 3: capped census + declared omission with group-by-kind counts.
    keep = len(bare_rows)
    while keep > 0:

        omitted = symbols[keep:]
        counts: dict[str, int] = {}
        for s in omitted:
            counts[str(s["kind"])] = counts.get(str(s["kind"]), 0) + 1
        by_kind = " · ".join(
            f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        omission = f"  … +{len(omitted)} symbols omitted (budget): {by_kind}"
        candidate = _render(
            ["  (signatures omitted: budget)"] + bare_rows[:keep] + [omission]
        )
        if _fits(candidate):
            return candidate
        keep -= 1
    # Degenerate budget: pure census-of-census, still declared.
    counts = {}
    for s in symbols:
        counts[str(s["kind"])] = counts.get(str(s["kind"]), 0) + 1
    by_kind = " · ".join(
        f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return _render([f"  … +{len(symbols)} symbols omitted (budget): {by_kind}"])


# --------------------------------------------------------------------------
# backend chain
# --------------------------------------------------------------------------
def _extract(
    source: str, language: str | None, rel: str
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Run the backend chain; returns (symbols, imports, parser-name).
    Symbols carry name/kind/signature/range/scope (spans minted later)."""
    if language is not None:
        try:
            syms, imps = _tree_sitter_extract(source, language)
            return syms, imps, "tree-sitter"
        except BackendUnavailable:
            pass
        except Exception:
            pass  # a broken parse degrades to the next backend, never errors
        try:
            syms, imps = _ctags_extract(source, language, rel)
            return syms, imps, "ctags"
        except BackendUnavailable:
            pass
        except Exception:
            pass
    if language == "python":
        try:
            syms, imps = _ast_extract(source)
            return syms, imps, "ast"
        except SyntaxError:
            pass
    return [], [], "none"


def _sym(
    name: str, kind: str, signature: str, a: int, b: int, scope: str | None
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "signature": signature[:_SIG_MAX],
        "range": [a, b],
        "scope": scope,
        "span": None,
    }


# ------------------------------------------------------------- python ast
def _python_imports(source: str) -> list[str]:
    """Imported module names via stdlib ast; [] on any parse failure."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.append("." * node.level + (node.module or ""))
    return sorted(dict.fromkeys(m for m in mods if m))


def _ast_extract(source: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Exact python extraction: stdlib ast, precise end_lineno ranges."""
    tree = ast.parse(source)  # SyntaxError propagates to the chain
    lines = source.splitlines()

    def sig_of(node: ast.AST) -> str:
        ln = getattr(node, "lineno", 1)
        return lines[ln - 1].strip() if 1 <= ln <= len(lines) else ""

    symbols: list[dict[str, Any]] = []

    def visit(body: list[ast.stmt], scope: str | None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if scope else "function"
                symbols.append(
                    _sym(node.name, kind, sig_of(node), node.lineno,
                         int(node.end_lineno or node.lineno), scope)
                )
            elif isinstance(node, ast.ClassDef):
                symbols.append(
                    _sym(node.name, "class", sig_of(node), node.lineno,
                         int(node.end_lineno or node.lineno), scope)
                )
                visit(node.body, node.name)
            elif isinstance(node, ast.Try):
                # recurse into try/except/else/finally bodies -- defs nested there were invisible
                visit(node.body, scope)
                for handler in node.handlers:
                    visit(handler.body, scope)
                visit(node.orelse, scope)
                visit(node.finalbody, scope)
            elif isinstance(node, (ast.If, ast.With, ast.AsyncWith)):
                visit(node.body, scope)
                if isinstance(node, ast.If):
                    visit(node.orelse, scope)

    visit(tree.body, None)
    return symbols, _python_imports(source)


# ------------------------------------------------------------------ ctags
def _ctags_path() -> str | None:
    """Seam for tests: universal-ctags binary on PATH, or None.

    ``CTX_NO_CTAGS`` is the escape hatch for a ctags install that is broken,
    pathologically slow, or simply unwanted; it must hold for *every* path
    that would shell out to ctags, this one and ``repomap._ctags_enabled``.
    Returning None here disables the backend the same way an absent binary
    does, so the chain degrades to ast/none instead of erroring."""
    if os.environ.get("CTX_NO_CTAGS"):
        return None
    return shutil.which("ctags")


def _run_ctags(argv: list[str]) -> subprocess.CompletedProcess:
    """Seam for tests: one bounded ctags invocation."""
    return subprocess.run(argv, capture_output=True, timeout=30)


def _ctags_extract(
    source: str, language: str | None, rel: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """universal-ctags backend: JSON tags → symbols. Ranges are best-effort
    (``end`` field when the parser emits one, next-tag heuristic otherwise);
    imports come only from the python supplement. The snapshotted bytes are
    parsed via a temp copy so the skeleton always matches its source blob;
    no absolute path (temp or workspace) ever enters the result."""
    exe = _ctags_path()
    if not exe:
        raise BackendUnavailable("ctags not on PATH")
    suffix = Path(rel).suffix or ".txt"
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="ctx-skel-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(source.encode("utf-8"))
        proc = _run_ctags(
            [exe, "--output-format=json", "--fields=*", "--sort=no", "-o", "-", tmp]
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise BackendUnavailable(f"ctags failed: {e}") from e
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        raise BackendUnavailable(f"ctags exit {proc.returncode}")

    tags: list[dict[str, Any]] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("_type") != "tag":
            continue
        if "def" not in str(obj.get("roles", "def")):
            continue  # definitions only; reference tags never enter the census
        if not obj.get("name") or not isinstance(obj.get("line"), int):
            continue
        tags.append(obj)
    tags.sort(key=lambda t: (t["line"], str(t["name"])))

    lines = source.splitlines()
    n = len(lines)
    starts = sorted({t["line"] for t in tags})
    symbols: list[dict[str, Any]] = []
    for t in tags:
        a = int(t["line"])
        end = t.get("end")
        if isinstance(end, int) and end >= a:
            b = end
        else:
            nxt = next((s for s in starts if s > a), None)
            b = (nxt - 1) if nxt else (n or a)
        scope = str(t["scope"]).split(".")[-1] if t.get("scope") else None
        sig = lines[a - 1].strip() if 1 <= a <= n else str(t["name"])
        symbols.append(_sym(str(t["name"]), str(t.get("kind", "unknown")), sig, a, b, scope))

    # Container extension: a parent without an ``end`` field must at least
    # cover its scoped children (two passes handle one nesting level each).
    for _ in range(2):
        for child in symbols:
            if not child["scope"]:
                continue
            parents = [
                p for p in symbols
                if p["name"] == child["scope"] and p["range"][0] <= child["range"][0]
            ]
            if parents:
                parent = max(parents, key=lambda p: p["range"][0])
                parent["range"][1] = max(parent["range"][1], child["range"][1])

    imports = _python_imports(source) if language == "python" else []
    return symbols, imports


# ------------------------------------------------------------ tree-sitter
_TS_PACK_NAMES = {
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "go": "go", "rust": "rust", "c": "c", "c++": "cpp", "c#": "csharp",
    "java": "java", "kotlin": "kotlin", "lua": "lua", "php": "php",
    "ruby": "ruby", "scala": "scala", "shell": "bash", "swift": "swift",
}


# Individual grammar wheels for the modern tree-sitter API (0.22+): each
# ``tree_sitter_<lang>`` module exposes ``language()`` (a PyCapsule) that
# ``tree_sitter.Language`` wraps. This is the maintained path — the bundle
# packages (language_pack, languages) lag the core API and, in sandboxed
# environments, language_pack fetches parsers at runtime (a network 403
# here). Grammar wheels are self-contained. Every language ``language_for``
# can name has a wheel here: the roster used to claim sixteen languages and
# ship grammars for five, so a C, Java or Ruby file silently fell to ctags
# (absent on most machines) and then to nothing.
_TS_GRAMMAR_MODULES = {
    "python": ("tree_sitter_python",),
    "javascript": ("tree_sitter_javascript",),
    # tree-sitter-typescript exposes two grammars under one module.
    "typescript": ("tree_sitter_typescript",),
    "go": ("tree_sitter_go",),
    "rust": ("tree_sitter_rust",),
    "c": ("tree_sitter_c",),
    "c++": ("tree_sitter_cpp",),
    "c#": ("tree_sitter_c_sharp",),
    "java": ("tree_sitter_java",),
    "kotlin": ("tree_sitter_kotlin",),
    "lua": ("tree_sitter_lua",),
    # tree-sitter-php exposes language_php (with HTML) and language_php_only.
    "php": ("tree_sitter_php",),
    "ruby": ("tree_sitter_ruby",),
    "scala": ("tree_sitter_scala",),
    "shell": ("tree_sitter_bash",),
    "swift": ("tree_sitter_swift",),
}
#: Grammar modules whose entry point is not the plain ``language()``.
_TS_LANGUAGE_FN = {"tree_sitter_typescript": "language_typescript", "tree_sitter_php": "language_php"}


def _ts_grammar_parser(language: str):
    """Parser from an individual ``tree_sitter_<lang>`` grammar wheel via
    the modern core API, or None when no grammar wheel is importable."""
    mods = _TS_GRAMMAR_MODULES.get(language)
    if not mods:
        return None
    try:
        import tree_sitter as _ts
    except Exception:
        return None
    for mod_name in mods:
        try:
            mod = __import__(mod_name)
            lang_fn = getattr(mod, _TS_LANGUAGE_FN.get(mod_name, "language"), None)
            if lang_fn is None:
                continue
            return _ts.Parser(_ts.Language(lang_fn()))
        except Exception:
            continue
    return None


def _ts_parser(language: str):
    name = _TS_PACK_NAMES.get(language)
    if name is None:
        raise BackendUnavailable(f"tree-sitter: no query set for {language!r}")
    # Preferred: the individual grammar wheels — this file's own
    # _TS_GRAMMAR_MODULES note already calls them "the maintained, offline
    # path", and says the bundles lag the core API and that language_pack
    # fetches parsers at runtime (a network 403 in a sandbox). The selection
    # order contradicted that note and tried the lagging, network-dependent
    # bundles first; a stale bundle that imports cleanly would win over a
    # current wheel. The bundles remain a fallback for languages no wheel is
    # declared for.
    parser = _ts_grammar_parser(language)
    if parser is not None:
        return parser
    for mod_name in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            mod = __import__(mod_name)
            return mod.get_parser(name)
        except Exception:
            continue
    raise BackendUnavailable("tree-sitter bindings not importable ([code] extra)")


# A declarative walker for the languages whose declarations all follow the
# same shape (a declaration node, a ``name`` field, an optional body that
# nests more declarations). Node and field names below were read off the
# grammars themselves (tests/test_skeleton_languages.py parses one fixture
# per language), not remembered. Per language:
#   decl        node type -> kind. "function" becomes "method" under a scope.
#   containers  node types whose body (field ``body``) is walked with the
#               declaration's name as the scope.
#   transparent node types walked through without changing scope (template
#               wrappers, namespaces declared without a body, export lists,
#               and tree-sitter's ERROR nodes, so a partial parse still
#               yields the symbols around the error).
#   imports     node type -> how to read the imported name.
_GENERIC_SPECS: dict[str, dict[str, Any]] = {
    "c": {
        "decl": {"function_definition": "function", "struct_specifier": "struct",
                 "union_specifier": "union", "enum_specifier": "enum", "type_definition": "type",
                 "declaration": "function"},
        "containers": {},
        "transparent": {"preproc_ifdef", "preproc_if", "preproc_else", "linkage_specification"},
        "imports": {"preproc_include": "path"},
    },
    "c++": {
        "decl": {"function_definition": "function", "class_specifier": "class",
                 "struct_specifier": "struct", "union_specifier": "union", "enum_specifier": "enum",
                 "type_definition": "type", "alias_declaration": "type",
                 "declaration": "function", "field_declaration": "function"},
        "containers": {"class_specifier", "struct_specifier", "namespace_definition"},
        "transparent": {"template_declaration", "preproc_ifdef", "preproc_if", "preproc_else",
                        "linkage_specification"},
        "imports": {"preproc_include": "path"},
    },
    "c#": {
        "decl": {"class_declaration": "class", "interface_declaration": "interface",
                 "struct_declaration": "struct", "enum_declaration": "enum",
                 "record_declaration": "record", "method_declaration": "method",
                 "constructor_declaration": "method", "property_declaration": "property",
                 "delegate_declaration": "type"},
        "containers": {"class_declaration", "interface_declaration", "struct_declaration",
                       "record_declaration", "namespace_declaration"},
        "transparent": {"file_scoped_namespace_declaration"},
        "imports": {"using_directive": "text:using"},
    },
    "java": {
        "decl": {"class_declaration": "class", "interface_declaration": "interface",
                 "enum_declaration": "enum", "record_declaration": "record",
                 "annotation_type_declaration": "type", "method_declaration": "method",
                 "constructor_declaration": "method"},
        "containers": {"class_declaration", "interface_declaration", "enum_declaration",
                       "record_declaration"},
        "transparent": set(),
        "imports": {"import_declaration": "text:import"},
    },
    "kotlin": {
        "decl": {"class_declaration": "class", "object_declaration": "object",
                 "function_declaration": "function", "type_alias": "type"},
        "containers": {"class_declaration", "object_declaration"},
        "transparent": set(),
        "imports": {"import": "text:import"},
    },
    "lua": {
        "decl": {"function_declaration": "function"},
        "containers": {},
        "transparent": set(),
        "imports": {},
    },
    "php": {
        "decl": {"class_declaration": "class", "interface_declaration": "interface",
                 "trait_declaration": "trait", "enum_declaration": "enum",
                 "function_definition": "function", "method_declaration": "method"},
        "containers": {"class_declaration", "interface_declaration", "trait_declaration",
                       "enum_declaration", "namespace_definition"},
        "transparent": set(),
        "imports": {"namespace_use_declaration": "text:use"},
    },
    "ruby": {
        "decl": {"class": "class", "module": "module", "method": "function",
                 "singleton_method": "function"},
        "containers": {"class", "module"},
        "transparent": set(),
        "imports": {"call": "ruby-require"},
    },
    "scala": {
        "decl": {"class_definition": "class", "object_definition": "object",
                 "trait_definition": "trait", "function_definition": "function",
                 "function_declaration": "function", "type_definition": "type"},
        "containers": {"class_definition", "object_definition", "trait_definition"},
        "transparent": set(),
        "imports": {"import_declaration": "text:import"},
    },
    "shell": {
        "decl": {"function_definition": "function"},
        "containers": {},
        "transparent": set(),
        "imports": {"command": "shell-source"},
    },
    "swift": {
        "decl": {"class_declaration": "class", "protocol_declaration": "protocol",
                 "function_declaration": "function", "init_declaration": "method",
                 "typealias_declaration": "type"},
        "containers": {"class_declaration", "protocol_declaration"},
        "transparent": set(),
        "imports": {"import_declaration": "text:import"},
    },
}
#: Swift folds class/struct/enum/actor/extension into one node type; the
#: keyword child says which. Kotlin does the same for interface.
_KEYWORD_KINDS = {"class", "struct", "enum", "actor", "extension", "interface", "object"}
_IDENTIFIER_TYPES = {"identifier", "field_identifier", "type_identifier", "qualified_identifier",
                     "destructor_name", "operator_name", "namespace_identifier", "constant",
                     "simple_identifier", "name", "word"}
#: Body node types, for grammars whose declaration bodies are children but
#: not a ``body`` field (Kotlin's class_body, C#'s declaration_list).
_BODY_TYPES = {"class_body", "declaration_list", "enum_body", "interface_body", "enum_class_body",
               "field_declaration_list", "template_body", "body_statement", "protocol_body",
               "enum_declaration_list", "block"}


def _body_of(node):
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for ch in node.children:
        if ch.type in _BODY_TYPES:
            return ch
    return None


def _c_declarator_name(node, text) -> str | None:
    """The declared name behind a C/C++ ``declarator`` chain, or None when
    the declaration is not a function (a plain variable, a field)."""
    cur = node.child_by_field_name("declarator")
    seen_function = node.type == "function_definition"
    while cur is not None:
        if cur.type == "function_declarator":
            seen_function = True
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            break
        cur = nxt
    if cur is None or not seen_function:
        return None
    if cur.type in _IDENTIFIER_TYPES:
        name = text(cur)
        return name.rsplit("::", 1)[-1] if "::" in name else name
    return None


def _generic_walk(root, language: str, text, add, imports: list[str]) -> None:
    spec = _GENERIC_SPECS[language]
    decl, containers = spec["decl"], spec["containers"]
    transparent, import_rules = spec["transparent"], spec["imports"]

    def keyword_kind(node) -> str | None:
        for ch in node.children:
            if not ch.is_named and ch.type in _KEYWORD_KINDS:
                return ch.type
        return None

    def name_of(node) -> str | None:
        if language in ("c", "c++") and node.type in (
            "function_definition", "declaration", "field_declaration"
        ):
            return _c_declarator_name(node, text)
        if node.type == "type_definition":  # C typedef: the name is the declarator
            d = node.child_by_field_name("declarator")
            return text(d) if d is not None else None
        child = node.child_by_field_name("name")
        if child is None:
            return None
        name = text(child).strip()
        if language == "lua":
            name = name.replace(":", ".")
        return name or None

    def record_import(node) -> None:
        rule = import_rules.get(node.type)
        if rule is None:
            return
        if rule == "path":
            p = node.child_by_field_name("path")
            if p is not None:
                imports.append(text(p).strip("<>\"' "))
        elif rule.startswith("text:"):
            raw = text(node).split("\n", 1)[0].strip().rstrip(";")
            for kw in (rule[5:], "static"):
                if raw.startswith(kw + " "):
                    raw = raw[len(kw) + 1:].strip()
            imports.append(raw)
        elif rule == "ruby-require":
            kids = node.named_children
            if kids and kids[0].type == "identifier" and text(kids[0]) in ("require", "require_relative"):
                for arg in kids[1:]:
                    if arg.type == "argument_list" and arg.named_children:
                        imports.append(text(arg.named_children[0]).strip("\"'"))
        elif rule == "shell-source":
            kids = node.named_children
            if len(kids) >= 2 and kids[0].type == "command_name" and text(kids[0]) in ("source", "."):
                imports.append(text(kids[1]).strip("\"'"))

    def walk(node, scope: str | None, in_type: bool) -> None:
        # ``in_type``: the enclosing container is a type (class, struct,
        # trait, object ...), so a function here is a method. A namespace or
        # module is a scope but not a type: functions in it stay functions.
        for child in node.children:
            t = child.type
            if t == "ERROR" or t in transparent:
                walk(child, scope, in_type)
                continue
            if t in import_rules:
                record_import(child)
                continue
            if t not in decl:
                if t in containers:
                    # A scoping container that is not a symbol of its own
                    # (a namespace): its body is walked under its name. One
                    # declared without a body (`namespace App;`) has nothing
                    # to descend into and file-level symbols keep None.
                    body = _body_of(child)
                    if body is not None:
                        walk(body, name_of(child) or scope, False)
                continue
            name = name_of(child)
            if not name:
                continue
            kind = decl[t]
            kw = keyword_kind(child) if t in ("class_declaration", "object_declaration") else None
            if kw and kw != "class":
                kind = kw
            if kind == "function" and in_type:
                kind = "method"
            add(child, name, kind, scope)
            if t in containers:
                body = _body_of(child)
                if body is not None:
                    walk(body, name, kind not in ("function", "method"))

    walk(root, None, False)


def _tree_sitter_extract(source: str, language: str) -> tuple[list[dict[str, Any]], list[str]]:
    """tree-sitter backend for python/javascript/typescript. Manual tree
    walks over stable node types (version-proof across Query API churn)."""
    parser = _ts_parser(language)
    data = source.encode("utf-8")
    tree = parser.parse(data)
    root = tree.root_node

    def text(node) -> str:
        return data[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def rng(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def first_line(node) -> str:
        return text(node).split("\n", 1)[0].strip()

    symbols: list[dict[str, Any]] = []
    imports: list[str] = []

    def add(node, name: str, kind: str, scope: str | None) -> None:
        a, b = rng(node)
        symbols.append(_sym(name, kind, first_line(node), a, b, scope))

    def name_of(node) -> str | None:
        child = node.child_by_field_name("name")
        return text(child) if child is not None else None

    if language == "python":

        def walk_py(node, scope: str | None) -> None:
            for child in node.children:
                t = child.type
                if t == "decorated_definition":
                    inner = child.child_by_field_name("definition")
                    if inner is not None:
                        handle_py(inner, scope)
                elif t in ("function_definition", "class_definition"):
                    handle_py(child, scope)
                elif t == "import_statement":
                    for sub in child.named_children:
                        if sub.type == "dotted_name":
                            imports.append(text(sub))
                        elif sub.type == "aliased_import":
                            target = sub.child_by_field_name("name")
                            if target is not None:
                                imports.append(text(target))
                elif t == "import_from_statement":
                    mod = child.child_by_field_name("module_name")
                    if mod is not None:
                        imports.append(text(mod))

        def handle_py(node, scope: str | None) -> None:
            name = name_of(node)
            if not name:
                return
            if node.type == "function_definition":
                add(node, name, "method" if scope else "function", scope)
            elif node.type == "class_definition":
                add(node, name, "class", scope)
                body = node.child_by_field_name("body")
                if body is not None:
                    walk_py(body, name)

        walk_py(root, None)
    elif language == "go":

        def _go_imports(node) -> None:
            if node.type == "import_spec":
                p = node.child_by_field_name("path")
                if p is not None:
                    imports.append(text(p).strip("`\"'"))
            else:
                for c in node.named_children:
                    _go_imports(c)

        def walk_go(node, scope: str | None) -> None:
            for child in node.children:
                t = child.type
                if t == "function_declaration":
                    name = name_of(child)
                    if name:
                        add(child, name, "function", scope)
                elif t == "method_declaration":
                    name = name_of(child)
                    if name:
                        add(child, name, "method", scope)
                elif t == "type_declaration":
                    for spec in child.named_children:
                        if spec.type == "type_spec":
                            name = name_of(spec)
                            if name:
                                add(spec, name, "type", scope)
                elif t == "import_declaration":
                    _go_imports(child)

        walk_go(root, None)
    elif language == "rust":

        def walk_rust(node, scope: str | None) -> None:
            for child in node.children:
                t = child.type
                if t == "function_item":
                    name = name_of(child)
                    if name:
                        add(child, name, "method" if scope else "function", scope)
                elif t == "struct_item":
                    name = name_of(child)
                    if name:
                        add(child, name, "struct", scope)
                elif t == "enum_item":
                    name = name_of(child)
                    if name:
                        add(child, name, "enum", scope)
                elif t == "trait_item":
                    name = name_of(child)
                    if name:
                        add(child, name, "trait", scope)
                        body = child.child_by_field_name("body")
                        if body is not None:
                            walk_rust(body, name)
                elif t == "impl_item":
                    ty = child.child_by_field_name("type")
                    cls = text(ty) if ty is not None else scope
                    body = child.child_by_field_name("body")
                    if body is not None:
                        walk_rust(body, cls)
                elif t == "mod_item":
                    name = name_of(child)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        walk_rust(body, name or scope)
                elif t == "use_declaration":
                    arg = child.child_by_field_name("argument")
                    if arg is not None:
                        imports.append(text(arg))

        walk_rust(root, None)
    elif language in _GENERIC_SPECS:
        _generic_walk(root, language, text, add, imports)
    else:  # javascript / typescript
        classy = {"class_declaration", "abstract_class_declaration"}
        fn_values = {"arrow_function", "function_expression", "function", "generator_function"}

        def walk_js(node, scope: str | None) -> None:
            for child in node.children:
                t = child.type
                if t == "export_statement":
                    walk_js(child, scope)
                elif t in ("function_declaration", "generator_function_declaration"):
                    name = name_of(child)
                    if name:
                        add(child, name, "function", scope)
                elif t in classy:
                    name = name_of(child)
                    if name:
                        add(child, name, "class", scope)
                        body = child.child_by_field_name("body")
                        if body is not None:
                            walk_class(body, name)
                elif t == "interface_declaration":
                    name = name_of(child)
                    if name:
                        add(child, name, "interface", scope)
                        body = child.child_by_field_name("body")
                        if body is not None:
                            walk_iface(body, name)
                elif t == "type_alias_declaration":
                    name = name_of(child)
                    if name:
                        add(child, name, "type", scope)
                elif t == "enum_declaration":
                    name = name_of(child)
                    if name:
                        add(child, name, "enum", scope)
                elif t in ("lexical_declaration", "variable_declaration"):
                    for decl in child.named_children:
                        if decl.type != "variable_declarator":
                            continue
                        name = name_of(decl)
                        if not name:
                            continue
                        value = decl.child_by_field_name("value")
                        kind = (
                            "function"
                            if value is not None and value.type in fn_values
                            else "constant"
                        )
                        add(decl, name, kind, scope)
                elif t == "import_statement":
                    src = child.child_by_field_name("source")
                    if src is not None:
                        imports.append(text(src).strip("'\"`"))
                elif t in ("module", "internal_module", "namespace_declaration"):
                    name = name_of(child)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        walk_js(body, name or scope)

        def walk_class(body, cls: str) -> None:
            for member in body.children:
                if member.type == "method_definition":
                    name = name_of(member)
                    if name:
                        add(member, name, "method", cls)
                elif member.type == "public_field_definition":
                    name = name_of(member)
                    if not name:
                        continue
                    value = member.child_by_field_name("value")
                    kind = (
                        "method"
                        if value is not None and value.type in fn_values
                        else "property"
                    )
                    add(member, name, kind, cls)

        def walk_iface(body, iface: str) -> None:
            for member in body.children:
                if member.type in ("property_signature", "method_signature"):
                    name = name_of(member)
                    if name:
                        kind = "method" if member.type == "method_signature" else "property"
                        add(member, name, kind, iface)

        walk_js(root, None)

    return symbols, sorted(dict.fromkeys(i for i in imports if i))
