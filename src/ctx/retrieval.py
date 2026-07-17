"""Bounded retrieval: ``ctx search``, ``ctx get``, ``ctx stats`` (SPEC §6.3-6.5).

Every result is deterministic, budget-capped, provenance-bearing, and — for
repository targets — snapshot-on-read so evidence stays retrievable after
the working tree changes.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from ctx.execution import snapshot_file
from ctx.refs import Ref, parse_ref
from ctx.store import Store
from ctx.textutil import bounded, estimate_tokens, fmt_bytes, fmt_int, sanitize_for_model
from ctx.workspace import Workspace


class RetrievalError(Exception):
    pass


# --------------------------------------------------------------------- util
def _glob_match(rel: str, glob: str) -> bool:
    """fnmatch with the convention that ``**/x`` also matches top-level ``x``."""
    if fnmatch.fnmatch(rel, glob):
        return True
    return glob.startswith("**/") and fnmatch.fnmatch(rel, glob[3:])



def _emit(ws: Workspace, text: str, budget_tokens: int, continuation: str | None = None) -> str:
    text, redactions = sanitize_for_model(text, ws.config.redaction.patterns)
    if redactions:
        text += "\nredaction: applied [" + ", ".join(redactions) + "]"
    return bounded(text, budget_tokens, continuation)


def _stream_lines(store: Store, blob_ref: str) -> list[str]:
    data = store.get_blob(str(blob_ref).removeprefix("sha256:"))
    if b"\x00" in data[:8192]:
        raise RetrievalError("binary stream: use --bytes selectors on blob content")
    return data.decode("utf-8", "replace").splitlines()


@dataclass
class SearchTarget:
    """One searchable unit: a named source with lines and a citation prefix."""

    label: str  # e.g. run:ab12cd34ef56#stdout or repo path
    lines: list[str]
    snapshot_id: str | None = None


def _resolve_run_targets(store: Store, ref: Ref) -> list[SearchTarget]:
    manifest = store.get_manifest(ref.id or "")
    short = str(manifest["id"]).removeprefix("sha256:")[:12]
    names = [ref.stream] if ref.stream else ["stdout", "stderr"]
    targets = []
    for name in names:
        meta = manifest["streams"].get(name)
        if not meta or not meta["bytes"]:
            continue
        targets.append(
            SearchTarget(label=f"run:{short}#{name}", lines=_stream_lines(store, meta["blob"]))
        )
    return targets


def _resolve_repo_targets(
    store: Store,
    ws: Workspace,
    ref: Ref,
    *,
    glob: str | None,
    scope: str | None,
    max_files: int = 5000,
    snapshot: bool = True,
) -> tuple[list[SearchTarget], int, int]:
    """Returns (targets, files_considered, files_skipped_binary)."""
    roots: list[str | None]
    if scope:
        scoped = ws.config.scopes.get(scope)
        if not scoped:
            raise RetrievalError(
                f"unknown scope {scope!r}; configured: {sorted(ws.config.scopes) or 'none'}"
            )
        roots = list(scoped)
    else:
        roots = [ref.path]

    rels: list[str] = []
    for root in roots:
        rels.extend(ws.list_files(root))
    rels = sorted(dict.fromkeys(rels))
    if glob:
        rels = [r for r in rels if _glob_match(r, glob)]
    rels = rels[:max_files]

    targets: list[SearchTarget] = []
    skipped_binary = 0
    for rel in rels:
        full = ws.confine(rel)
        try:
            data = full.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            skipped_binary += 1
            continue
        text = data.decode("utf-8", "replace")
        targets.append(SearchTarget(label=rel, lines=text.splitlines()))
    return targets, len(rels), skipped_binary


# ------------------------------------------------------------------- search
@dataclass
class Match:
    target: str
    line_no: int
    pattern_index: int
    line: str


def search(
    store: Store,
    ws: Workspace,
    ref_text: str,
    patterns: list[str],
    *,
    fixed: bool = False,
    mode_all: bool = False,
    context: int = 0,
    glob: str | None = None,
    scope: str | None = None,
    max_matches: int | None = None,
) -> str:
    """Multi-pattern bounded search with deterministic ordering by
    (target, coordinate, pattern index) and explicit coverage reporting."""
    if not patterns:
        raise RetrievalError("at least one pattern is required")
    ref = _parse(ref_text)
    budget = ws.config.budgets
    cap = max_matches or budget.max_matches

    flags = 0
    try:
        rxs = [
            re.compile(re.escape(p) if fixed else p, flags)
            for p in patterns
        ]
    except re.error as e:
        raise RetrievalError(f"invalid pattern: {e}") from e

    snapshot_note: list[str] = []
    if ref.kind == "run":
        targets = _resolve_run_targets(store, ref)
        considered, skipped_binary = len(targets), 0
    elif ref.kind == "blob":
        blob_id = store.resolve_id(ref.id or "", kinds=("blob",))
        targets = [SearchTarget(label=f"blob:{blob_id[:12]}", lines=_stream_lines(store, blob_id))]
        considered, skipped_binary = 1, 0
    elif ref.kind == "repo":
        targets, considered, skipped_binary = _resolve_repo_targets(
            store, ws, ref, glob=glob, scope=scope
        )
    else:
        raise RetrievalError(f"cannot search reference kind {ref.kind!r}")

    matches: list[Match] = []
    scanned_lines = 0
    matched_targets: set[str] = set()
    for target in targets:
        scanned_lines += len(target.lines)
        if mode_all:
            # every pattern must appear somewhere in the target
            if not all(any(rx.search(ln) for ln in target.lines) for rx in rxs):
                continue
        for i, ln in enumerate(target.lines, start=1):
            for pi, rx in enumerate(rxs):
                if rx.search(ln):
                    matches.append(Match(target.label, i, pi, ln))
                    matched_targets.add(target.label)
                    break  # one match record per line; pattern index is the first hit

    matches.sort(key=lambda m: (m.target, m.line_no, m.pattern_index))
    shown = matches[:cap]

    # Snapshot-on-read for repo evidence (SPEC §6.3).
    if ref.kind == "repo" and shown:
        for label in sorted({m.target for m in shown}):
            try:
                snap = snapshot_file(store, ws, label)
                snapshot_note.append(
                    f"  {label} → snapshot:{str(snap['id']).removeprefix('sha256:')[:12]}"
                )
            except Exception:
                pass

    out: list[str] = [f"[ctx search {ref.display()}]"]
    out.append("patterns: " + " ".join(repr(p) for p in patterns) + (" (all)" if mode_all else " (any)"))
    last_target = None
    for m in shown:
        if m.target != last_target:
            out.append(f"{m.target}:")
            last_target = m.target
        target_obj = next(t for t in targets if t.label == m.target)
        a = max(1, m.line_no - context)
        b = min(len(target_obj.lines), m.line_no + context)
        if context:
            for ln_no in range(a, b + 1):
                marker = ">" if ln_no == m.line_no else " "
                out.append(f" {marker}L{ln_no}: {target_obj.lines[ln_no - 1][:200]}")
        else:
            out.append(f"  L{m.line_no}: {m.line[:200]}")

    out.append("coverage:")
    out.append(
        f"  scanned: {fmt_int(considered)} targets · {fmt_int(scanned_lines)} lines"
        + (f" · {skipped_binary} binary skipped" if skipped_binary else "")
    )
    out.append(
        f"  matches: {fmt_int(len(matches))} · shown: {fmt_int(len(shown))}"
        + (" · truncated" if len(matches) > len(shown) else "")
    )
    if snapshot_note:
        out.append("snapshots:")
        out.extend(snapshot_note)

    continuation = None
    if len(matches) > len(shown):
        continuation = f"ctx search {ref_text} … --max-matches {min(len(matches), cap * 2)}"
    return _emit(ws, "\n".join(out), budget.result_tokens, continuation)


# ---------------------------------------------------------------------- get
@dataclass
class Selector:
    lines: tuple[int, int] | None = None
    bytes: tuple[int, int] | None = None
    records: tuple[int, int] | None = None
    json_pointer: str | None = None


def _parse(ref_text: str) -> Ref:
    return parse_ref(ref_text)


def _span(spec: str) -> tuple[int, int]:
    m = re.match(r"^(\d+):(\d+)$", spec.strip())
    if not m:
        raise RetrievalError(f"invalid span {spec!r}; expected A:B")
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        raise RetrievalError(f"invalid span {spec!r}: need 1 <= A <= B")
    return a, b


def get(
    store: Store,
    ws: Workspace,
    ref_text: str,
    selector: Selector,
) -> str:
    """Exact bounded slice with provenance. Oversized requests return a
    bounded preview plus continuation coordinates (never silent flooding)."""
    ref = _parse(ref_text)
    budget = ws.config.budgets

    label: str
    data: bytes
    divergence: str | None = None

    if ref.kind == "run":
        manifest = store.get_manifest(ref.id or "")
        short = str(manifest["id"]).removeprefix("sha256:")[:12]
        stream = ref.stream or "stdout"
        meta = manifest["streams"].get(stream)
        if meta is None:
            raise RetrievalError(f"run:{short} has no stream {stream!r}")
        data = store.get_blob(str(meta["blob"]).removeprefix("sha256:"))
        label = f"run:{short}#{stream}"
    elif ref.kind in ("blob", "snapshot"):
        if ref.kind == "snapshot":
            manifest = store.get_manifest(ref.id or "")
            data = store.get_blob(str(manifest["blob"]).removeprefix("sha256:"))
            label = f"snapshot:{str(manifest['id']).removeprefix('sha256:')[:12]} ({manifest.get('path', '?')})"
            # Label divergence when the current worktree differs (SPEC §15).
            try:
                current = ws.confine(str(manifest["path"])).read_bytes()
                if current != data:
                    divergence = "current worktree file differs from this snapshot"
            except Exception:
                divergence = "file no longer present in worktree"
        else:
            blob_id = store.resolve_id(ref.id or "", kinds=("blob",))
            data = store.get_blob(blob_id)
            label = f"blob:{blob_id[:12]}"
    elif ref.kind == "repo":
        if not ref.path:
            raise RetrievalError("get repo: requires a file path (repo:<path>)")
        snap = snapshot_file(store, ws, ref.path)
        data = store.get_blob(str(snap["blob"]).removeprefix("sha256:"))
        label = f"repo:{ref.path} (snapshot:{str(snap['id']).removeprefix('sha256:')[:12]})"
    else:
        raise RetrievalError(f"cannot get reference kind {ref.kind!r}")

    header = [f"[ctx get {label}]"]
    body: str
    continuation: str | None = None

    if selector.json_pointer is not None:
        try:
            doc = json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise RetrievalError(f"content is not JSON: {e}") from e
        node: Any = doc
        pointer = selector.json_pointer
        if pointer not in ("", "/"):
            for token in pointer.lstrip("/").split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(node, list):
                    try:
                        node = node[int(token)]
                    except (ValueError, IndexError):
                        raise RetrievalError(f"json-pointer not found: {pointer}") from None
                elif isinstance(node, dict) and token in node:
                    node = node[token]
                else:
                    raise RetrievalError(f"json-pointer not found: {pointer}")
        header.append(f"selector: --json-pointer {pointer or '/'}")
        body = json.dumps(node, indent=2, sort_keys=True, ensure_ascii=False)
    elif selector.records is not None:
        a, b = selector.records
        lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
        header.append(f"selector: --records {a}:{b} of {fmt_int(len(lines))}")
        body = "\n".join(lines[a - 1 : b])
        if b < len(lines):
            continuation = f"ctx get {ref_text} --records {b + 1}:{min(len(lines), b + (b - a + 1))}"
    elif selector.bytes is not None:
        a, b = selector.bytes
        chunk = data[a - 1 : b]
        header.append(f"selector: --bytes {a}:{b} of {fmt_int(len(data))}")
        body = chunk.decode("utf-8", "replace")
        if b < len(data):
            continuation = f"ctx get {ref_text} --bytes {b + 1}:{min(len(data), b + (b - a + 1))}"
    else:
        if b"\x00" in data[:8192]:
            raise RetrievalError("binary content: use --bytes A:B for exact slices")
        all_lines = data.decode("utf-8", "replace").splitlines()
        if selector.lines is None:
            a, b = 1, min(len(all_lines), ws.config.budgets.max_inline_lines)
        else:
            a, b = selector.lines
        b = min(b, len(all_lines))
        requested = b - a + 1 if b >= a else 0
        if requested > budget.max_inline_lines:
            b = a + budget.max_inline_lines - 1
            continuation = f"ctx get {ref_text} --lines {b + 1}:{min(len(all_lines), b + budget.max_inline_lines)}"
        header.append(f"selector: --lines {a}:{b} of {fmt_int(len(all_lines))}")
        numbered = [f"L{n}: {all_lines[n - 1]}" for n in range(max(1, a), b + 1)]
        body = "\n".join(numbered)
        if continuation is None and b < len(all_lines) and selector.lines is not None:
            pass  # exact request satisfied; no continuation needed

    if divergence:
        header.append(f"divergence: {divergence}")
    return _emit(ws, "\n".join(header) + "\n" + body, budget.result_tokens, continuation)


# -------------------------------------------------------------------- stats
_LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cpp": "c++", ".hpp": "c++",
    ".cs": "c#", ".swift": "swift", ".sh": "shell", ".bash": "shell", ".sql": "sql",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".scss": "css", ".proto": "protobuf", ".tf": "terraform",
}


def stats(store: Store, ws: Workspace, ref_text: str, *, scope: str | None = None) -> str:
    ref = _parse(ref_text)
    budget = ws.config.budgets
    out: list[str] = []

    if ref.kind == "run":
        manifest = store.get_manifest(ref.id or "")
        short = str(manifest["id"]).removeprefix("sha256:")[:12]
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
                size = ws.confine(rel).stat().st_size
            except OSError:
                continue
            total_bytes += size
            ext = "." + rel.rsplit(".", 1)[-1] if "." in rel.rsplit("/", 1)[-1] else ""
            lang = _LANG_BY_EXT.get(ext.lower(), "other")
            langs[lang] = langs.get(lang, 0) + 1
            largest.append((size, rel))
        out.append(f"files (exact): {fmt_int(len(rels))} · {fmt_bytes(total_bytes)}")
        if ws.git:
            out.append(
                f"git (exact): HEAD {ws.git.head[:12] if ws.git.head else 'none'}"
                + (" · dirty" if ws.git.dirty else " · clean")
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


# ----------------------------------------------------- per-turn budget gate
def charge_turn_budget(store: Store, ws: Workspace, emitted_text: str) -> str | None:
    """Enforce the cumulative per-turn retrieval budget when conversation and
    turn identifiers are available (env-provided by the harness)."""
    import os

    conv = os.environ.get("CTX_CONVERSATION_ID")
    turn = os.environ.get("CTX_TURN_ID")
    if not conv or not turn:
        return None
    tokens = estimate_tokens(len(emitted_text.encode("utf-8")))
    total = store.add_turn_tokens(conv, turn, tokens)
    limit = ws.config.budgets.turn_retrieval_tokens
    if total > limit:
        return (
            f"[ctx budget] turn retrieval budget exceeded: ≈{total} of {limit} tokens. "
            "Narrow selectors, or checkpoint the conversation into a new epoch."
        )
    return None
