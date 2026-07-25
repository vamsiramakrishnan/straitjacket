"""``ctx stats``: shape stats for run/repo references, or a priced symbol
outline for a single Python file (SPEC §6.5, docs/PRICED-CONTEXT.md M2)."""

from __future__ import annotations

from ctx.execution import snapshot_file
from ctx.store import Store
from ctx.textutil import (
    estimate_tokens,
    fmt_bytes,
    fmt_int,
    fmt_tokens_coarse,
    short_id,
)
from ctx.workspace import Workspace

from .common import RetrievalError, _emit, _parse, _route_workspace

_LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cpp": "c++", ".hpp": "c++",
    ".cs": "c#", ".swift": "swift", ".sh": "shell", ".bash": "shell", ".sql": "sql",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".scss": "css", ".proto": "protobuf", ".tf": "terraform",
}


_OUTLINE_MAX_ENTRIES = 48


def _skeleton_stats_outline(
    store: Store, ws: Workspace, rel: str, budget_tokens: int
) -> str | None:
    """Priced outline for non-Python code files via the skeleton tier
    (docs/ALGEBRA.md M-F: tree-sitter → ctags → none). Returns None when no
    backend supports the file so the caller falls through to the generic
    stats path unchanged. Python never routes here — the existing exact
    ast outline above stays byte-identical."""
    from ctx.skeleton import language_for, skeleton_for, skeleton_outline

    if language_for(rel) in (None, "python"):
        return None
    try:
        sk = skeleton_for(store, ws, rel)
    except Exception:
        return None  # absence/parse trouble degrades, never errors
    if sk.get("parser") == "none" or not sk.get("symbols"):
        return None
    return _emit(ws, skeleton_outline(sk, budget_tokens), budget_tokens)


def _stats_outline(store: Store, ws: Workspace, rel: str) -> str:
    """Priced symbol outline for one code file: name · lines · ~tokens ·
    span handle per entry. Deterministic given file bytes; spans are minted
    against a snapshot so they stay stable if the worktree moves on."""
    import ast as _ast

    snap = snapshot_file(store, ws, rel)
    source = store.get_blob(str(snap["blob"]).removeprefix("sha256:")).decode(
        "utf-8", "replace"
    )
    lines = source.splitlines()
    out = [f"[ctx stats repo:{rel}]"]
    out.append(
        f"file (exact): {fmt_int(len(lines))} lines · {fmt_bytes(len(source.encode()))} "
        f"· est {fmt_tokens_coarse(estimate_tokens(len(source.encode())))} tok"
    )
    try:
        tree = _ast.parse(source)
    except SyntaxError as e:
        out.append(f"outline: unavailable (syntax error at line {e.lineno})")
        return "\n".join(out)

    entries: list[tuple[int, str, str]] = []  # (lineno, indent+name, kind)
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            entries.append((node.lineno, node.name, "def"))
        elif isinstance(node, _ast.ClassDef):
            entries.append((node.lineno, node.name, "class"))
            for sub in node.body:
                if isinstance(sub, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    entries.append((sub.lineno, f"{node.name}.{sub.name}", "def"))

    def _end(lineno: int, name: str) -> int:
        for node in _ast.walk(tree):
            if (
                isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))
                and node.lineno == lineno
            ):
                return int(node.end_lineno or node.lineno)
        return lineno

    out.append("outline (priced):")
    for lineno, name, kind in entries[:_OUTLINE_MAX_ENTRIES]:
        end = _end(lineno, name)
        seg_bytes = len("\n".join(lines[lineno - 1 : end]).encode())
        sid = store.register_span(str(snap["blob"]), "region", a=lineno, b=end)
        indent = "    " if "." in name else "  "
        out.append(
            f"{indent}{kind} {name} L{lineno}-{end} "
            f"{fmt_tokens_coarse(estimate_tokens(seg_bytes))} tok · span {sid}"
        )
    if len(entries) > _OUTLINE_MAX_ENTRIES:
        out.append(f"  … +{fmt_int(len(entries) - _OUTLINE_MAX_ENTRIES)} more symbols")
    if not entries:
        out.append("  (no top-level symbols)")
    out.append("next:")
    out.append(f"  ctx get repo:{rel} --symbol <Name.dotted>")
    out.append(f"  ctx get repo:{rel} --lines A:B")
    return "\n".join(out)


def stats(store: Store, ws: Workspace, ref_text: str, *, scope: str | None = None) -> str:
    ref = _parse(ref_text)
    store, ws = _route_workspace(store, ws, ref)
    budget = ws.config.budgets
    out: list[str] = []

    if ref.kind == "run":
        manifest = store.get_manifest(ref.id or "")
        short = short_id(manifest["id"])
        out.append(f"[ctx stats run:{short}]")
        out.append(f"cwd: {manifest['cwd']}")
        out.append(f"command: {' '.join(manifest['argv'])}")
        r = manifest["result"]
        out.append(
            f"result (exact): exit={r['exitCode']} signal={r['signal']} timedOut={r['timedOut']}"
        )
        for name in ("stdout", "stderr"):
            meta = manifest["streams"][name]
            out.append(
                f"{name} (exact): {fmt_int(meta['lines'])} lines · {fmt_bytes(meta['bytes'])} "
                f"· est {fmt_int(estimate_tokens(meta['bytes']))} tokens (approximate) · {meta['mediaType']}"
            )
        out.append(f"digest (exact): profile={manifest['digest']['profile']} policy={manifest['digest']['policy']}")
    elif ref.kind == "repo":
        # Priced symbol outline (docs/PRICED-CONTEXT.md, M2): stats on a
        # single structured file returns the menu — every entry carries its
        # own price and span handle, so degrading a read to this outline is
        # structured-lossy, not truncated-lossy. Measured 12.8–54.5× cheaper
        # than the file it describes.
        if ref.path and not scope and ref.path.endswith(".py"):
            target = ws.confine(ref.path, must_exist=False)
            if target.is_file():
                return _stats_outline(store, ws, ws.relativize(target))
        # M-F: the same priced-outline capability for non-Python code files,
        # via the skeleton tier. Additive only — files no backend can parse
        # fall through to the aggregate path exactly as before.
        if ref.path and not scope:
            target = ws.confine(ref.path, must_exist=False)
            if target.is_file():
                rendered = _skeleton_stats_outline(
                    store, ws, ws.relativize(target), budget.result_tokens
                )
                if rendered is not None:
                    return rendered
        rels = ws.list_files(ref.path) if not scope else None
        if scope:
            scoped = ws.config.scopes.get(scope)
            if not scoped:
                raise RetrievalError(f"unknown scope {scope!r}")
            rels = []
            for root in scoped:
                rels.extend(ws.list_files(root))
            rels = sorted(dict.fromkeys(rels))
        assert rels is not None
        out.append(f"[ctx stats {ref.display()}{' scope=' + scope if scope else ''}]")
        total_bytes = 0
        langs: dict[str, int] = {}
        largest: list[tuple[int, str]] = []
        for rel in rels:
            try:
                size = (ws.root / rel).stat().st_size
            except OSError:
                continue
            total_bytes += size
            ext = "." + rel.rsplit(".", 1)[-1] if "." in rel.rsplit("/", 1)[-1] else ""
            lang = _LANG_BY_EXT.get(ext.lower(), "other")
            langs[lang] = langs.get(lang, 0) + 1
            largest.append((size, rel))
        out.append(f"files (exact): {fmt_int(len(rels))} · {fmt_bytes(total_bytes)}")
        if ws.git:
            dirty = ws.git_dirty()
            state = " · dirty" if dirty else (" · clean" if dirty is not None else "")
            out.append(
                f"git (exact): HEAD {ws.git.head[:12] if ws.git.head else 'none'}" + state
            )
        out.append(
            "languages (exact): "
            + " · ".join(f"{k}:{v}" for k, v in sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
        )
        largest.sort(key=lambda t: (-t[0], t[1]))
        out.append("largest files (exact):")
        for size, rel in largest[:8]:
            out.append(f"  {rel} · {fmt_bytes(size)}")
        if ws.config.scopes and not scope:
            out.append("scopes (exact): " + ", ".join(sorted(ws.config.scopes)))
    else:
        raise RetrievalError(f"stats supports run: and repo: references, got {ref.kind!r}")

    return _emit(ws, "\n".join(out), budget.result_tokens)
