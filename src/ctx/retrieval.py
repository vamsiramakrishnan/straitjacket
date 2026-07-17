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


def _stream_text(store: Store, blob_ref: str) -> str:
    data = store.get_blob(str(blob_ref).removeprefix("sha256:"))
    if b"\x00" in data[:8192]:
        raise RetrievalError("binary stream: use --bytes selectors on blob content")
    return data.decode("utf-8", "replace")


@dataclass
class SearchTarget:
    """One searchable unit addressed by character offsets.

    All line geometry runs on C-speed primitives (``count``/``find``/
    ``rfind``) computed only where needed — no O(lines) Python loop, no
    line-start index materialization. Matching is ``finditer`` whole-text.
    """

    label: str  # e.g. run:ab12cd34ef56#stdout or repo path
    text: str

    @property
    def n_lines(self) -> int:
        if not self.text:
            return 0
        return self.text.count("\n") + (0 if self.text.endswith("\n") else 1)

    def line_start_of(self, char_pos: int) -> int:
        """Offset of the start of the line containing char_pos."""
        return self.text.rfind("\n", 0, char_pos) + 1

    def line_no_of(self, line_start: int) -> int:
        """1-indexed line number for a line-start offset (O(offset) memchr)."""
        return self.text.count("\n", 0, line_start) + 1

    def line_text_at(self, line_start: int) -> str:
        end = self.text.find("\n", line_start)
        return self.text[line_start : end if end != -1 else len(self.text)]

    def prev_line_start(self, line_start: int) -> int | None:
        if line_start <= 0:
            return None
        return self.text.rfind("\n", 0, line_start - 1) + 1

    def next_line_start(self, line_start: int) -> int | None:
        end = self.text.find("\n", line_start)
        if end == -1 or end + 1 >= len(self.text):
            return None
        return end + 1


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
            SearchTarget(label=f"run:{short}#{name}", text=_stream_text(store, meta["blob"]))
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
    root = ws.root
    for rel in rels:
        # rels come from ws.list_files (already confined + ignore-filtered);
        # skip per-file re-confinement syscalls on the hot loop.
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            skipped_binary += 1
            continue
        targets.append(SearchTarget(label=rel, text=data.decode("utf-8", "replace")))
    return targets, len(rels), skipped_binary


# ------------------------------------------------------------------- search
@dataclass
class Match:
    target: str
    line_start: int  # char offset; line number/text computed only if shown
    pattern_index: int


@dataclass
class RgMatch:
    target: str
    line_no: int
    pattern_index: int
    line: str


def _rg_available() -> bool:
    import os
    import shutil

    if os.environ.get("CTX_SEARCH_ENGINE") == "python":
        return False
    return shutil.which("rg") is not None


def _rg_repo_search(
    ws: Workspace,
    paths: list[str],
    patterns: list[str],
    rxs: list["re.Pattern[str]"],
    *,
    fixed: bool,
    glob: str | None,
) -> tuple[list[RgMatch], str, int] | None:
    """Repo search via ripgrep (SIMD prefilter, parallel walk, native
    gitignore). Returns (matches, coverage line) or None to fall back.

    Determinism: ``--sort path`` plus our final (target, line, pattern) sort.
    Ignore policy: rg's own .gitignore handling plus our deny globs; the
    pattern-index for ordering is recovered by re-matching the emitted line.
    """
    import json as _json
    import subprocess

    argv = ["rg", "--json", "--no-config", "--sort", "path", "--stats"]
    if fixed:
        argv.append("--fixed-strings")
    if not (ws.git is not None and ws.config.workspace.respect_gitignore):
        argv.append("--no-ignore")
    if not ws.config.workspace.follow_symlinks:
        pass  # rg does not follow symlinks by default
    argv.append("--hidden")  # parity with the Python engine's os.walk
    if glob:
        argv += ["--glob", glob]
    # Deny globs come after the include glob: rg gives the last matching
    # glob precedence, and capture exclusions must always win.
    argv += ["--glob", "!.git/**"]
    for deny in ws.ignore_globs:
        argv += ["--glob", f"!{deny}"]
    for p in patterns:
        argv += ["-e", p]
    argv += ["--"] + (paths or ["."])

    try:
        proc = subprocess.run(
            argv, cwd=ws.root, capture_output=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):  # 2 = error (bad pattern already caught)
        return None

    matches: list[RgMatch] = []
    scanned = ""
    bytes_searched = 0
    for raw_line in proc.stdout.splitlines():
        try:
            msg = _json.loads(raw_line)
        except _json.JSONDecodeError:
            continue
        mtype = msg.get("type")
        data = msg.get("data") or {}
        if mtype == "match":
            path_obj = data.get("path") or {}
            lines_obj = data.get("lines") or {}
            if "text" not in path_obj or "text" not in lines_obj:
                continue  # non-UTF-8 path/line: python engine handles via lossy decode
            line = lines_obj["text"].rstrip("\n")
            pi = next((i for i, rx in enumerate(rxs) if rx.search(line)), 0)
            rel = path_obj["text"]
            if rel.startswith("./"):
                rel = rel[2:]
            matches.append(
                RgMatch(
                    target=rel.replace("\\", "/"),
                    line_no=int(data.get("line_number") or 0),
                    pattern_index=pi,
                    line=line,
                )
            )
        elif mtype == "summary":
            stats = data.get("stats") or {}
            bytes_searched = int(stats.get("bytes_searched", 0))
            # rg's prefilter proves most files cannot match without a full
            # scan; coverage over the glob/ignore-filtered corpus is complete.
            scanned = (
                "  scanned: complete over corpus · "
                f"{fmt_int(int(stats.get('searches', 0)))} deep-searched · "
                f"{fmt_bytes(bytes_searched)}"
            )
    matches.sort(key=lambda m: (m.target, m.line_no, m.pattern_index))
    return matches, scanned or "  scanned: complete over corpus", bytes_searched


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
    store, ws = _route_workspace(store, ws, ref)
    budget = ws.config.budgets
    cap = max_matches or budget.max_matches

    # MULTILINE keeps ^/$ line-anchored now that matching runs whole-text.
    flags = re.MULTILINE
    try:
        rxs = [
            re.compile(re.escape(p) if fixed else p, flags)
            for p in patterns
        ]
    except re.error as e:
        raise RetrievalError(f"invalid pattern: {e}") from e

    snapshot_note: list[str] = []

    # -------- repo searches prefer ripgrep when installed (auto-fallback)
    if ref.kind == "repo" and _rg_available():
        if scope:
            scoped = ws.config.scopes.get(scope)
            if not scoped:
                raise RetrievalError(
                    f"unknown scope {scope!r}; configured: {sorted(ws.config.scopes) or 'none'}"
                )
            roots = list(scoped)
        else:
            roots = [ref.path] if ref.path else []
        for r in roots:
            ws.confine(r, must_exist=True)  # workspace confinement still applies
        rg_result = _rg_repo_search(
            ws, roots, patterns, rxs, fixed=fixed, glob=glob
        )
        if rg_result is not None:
            return _render_rg_search(
                store, ws, ref, ref_text, patterns, rg_result,
                mode_all=mode_all, context=context, cap=cap, rxs=rxs,
            )
        # else: fall through to the Python engine

    if ref.kind == "run":
        targets = _resolve_run_targets(store, ref)
        considered, skipped_binary = len(targets), 0
    elif ref.kind == "blob":
        blob_id = store.resolve_id(ref.id or "", kinds=("blob",))
        targets = [SearchTarget(label=f"blob:{blob_id[:12]}", text=_stream_text(store, blob_id))]
        considered, skipped_binary = 1, 0
    elif ref.kind == "repo":
        targets, considered, skipped_binary = _resolve_repo_targets(
            store, ws, ref, glob=glob, scope=scope
        )
    else:
        raise RetrievalError(f"cannot search reference kind {ref.kind!r}")

    matches: list[Match] = []
    scanned_lines = 0
    for target in targets:
        scanned_lines += target.n_lines
        if mode_all and not all(rx.search(target.text) for rx in rxs):
            continue
        # C-speed scan: finditer per pattern over the whole text; dedup to one
        # record per line via the line-start offset (lowest pattern index
        # wins). Line numbers are resolved later, only for shown matches.
        per_line: dict[int, int] = {}
        text = target.text
        for pi, rx in enumerate(rxs):
            for m in rx.finditer(text):
                ls = text.rfind("\n", 0, m.start()) + 1
                prev = per_line.get(ls)
                if prev is None or pi < prev:
                    per_line[ls] = pi
        for ls, pi in per_line.items():
            matches.append(Match(target.label, ls, pi))

    matches.sort(key=lambda m: (m.target, m.line_start, m.pattern_index))
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
    by_label = {t.label: t for t in targets}
    for m in shown:
        if m.target != last_target:
            out.append(f"{m.target}:")
            last_target = m.target
        t = by_label[m.target]
        line_no = t.line_no_of(m.line_start)
        if context:
            back: list[int] = []
            ls: int | None = m.line_start
            for _ in range(context):
                ls = t.prev_line_start(ls)  # type: ignore[arg-type]
                if ls is None:
                    break
                back.append(ls)
            back.reverse()
            fwd: list[int] = []
            ls = m.line_start
            for _ in range(context):
                ls = t.next_line_start(ls)  # type: ignore[arg-type]
                if ls is None:
                    break
                fwd.append(ls)
            for i, ls_k in enumerate(back):
                out.append(f"  L{line_no - len(back) + i}: {t.line_text_at(ls_k)[:200]}")
            out.append(f" >L{line_no}: {t.line_text_at(m.line_start)[:200]}")
            for i, ls_k in enumerate(fwd, start=1):
                out.append(f"  L{line_no + i}: {t.line_text_at(ls_k)[:200]}")
        else:
            out.append(f"  L{line_no}: {t.line_text_at(m.line_start)[:200]}")

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
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation)
    record_telemetry(
        store, "search", sum(len(t.text) for t in targets), len(result.encode("utf-8"))
    )
    return result


def _render_rg_search(
    store: Store,
    ws: Workspace,
    ref: Ref,
    ref_text: str,
    patterns: list[str],
    rg_result: tuple[list[RgMatch], str, int],
    *,
    mode_all: bool,
    context: int,
    cap: int,
    rxs: list["re.Pattern[str]"],
) -> str:
    matches, scanned_line, bytes_searched = rg_result
    budget = ws.config.budgets

    if mode_all and matches:
        by_target: dict[str, list[RgMatch]] = {}
        for m in matches:
            by_target.setdefault(m.target, []).append(m)
        keep: set[str] = set()
        for target, ms in by_target.items():
            lines = [m.line for m in ms]
            if all(any(rx.search(ln) for ln in lines) for rx in rxs):
                keep.add(target)
        matches = [m for m in matches if m.target in keep]

    shown = matches[:cap]

    out: list[str] = [f"[ctx search {ref.display()}]"]
    out.append(
        "patterns: " + " ".join(repr(p) for p in patterns) + (" (all)" if mode_all else " (any)")
    )

    # Context rendering reads only the files that actually appear in output.
    file_lines: dict[str, list[str]] = {}
    if context:
        for label in dict.fromkeys(m.target for m in shown):
            try:
                file_lines[label] = (
                    (ws.root / label).read_bytes().decode("utf-8", "replace").splitlines()
                )
            except OSError:
                file_lines[label] = []

    last_target = None
    for m in shown:
        if m.target != last_target:
            out.append(f"{m.target}:")
            last_target = m.target
        if context and file_lines.get(m.target):
            lines = file_lines[m.target]
            a = max(1, m.line_no - context)
            b = min(len(lines), m.line_no + context)
            for n in range(a, b + 1):
                marker = ">" if n == m.line_no else " "
                out.append(f" {marker}L{n}: {lines[n - 1][:200]}")
        else:
            out.append(f"  L{m.line_no}: {m.line[:200]}")

    out.append("coverage:")
    out.append(scanned_line)
    out.append(
        f"  matches: {fmt_int(len(matches))} · shown: {fmt_int(len(shown))}"
        + (" · truncated" if len(matches) > len(shown) else "")
    )

    snapshot_note: list[str] = []
    for label in sorted({m.target for m in shown}):
        try:
            snap = snapshot_file(store, ws, label)
            snapshot_note.append(
                f"  {label} → snapshot:{str(snap['id']).removeprefix('sha256:')[:12]}"
            )
        except Exception:
            pass
    if snapshot_note:
        out.append("snapshots:")
        out.extend(snapshot_note)

    continuation = None
    if len(matches) > len(shown):
        continuation = f"ctx search {ref_text} … --max-matches {min(len(matches), cap * 2)}"
    result = _emit(ws, "\n".join(out), budget.result_tokens, continuation)
    record_telemetry(store, "search", bytes_searched, len(result.encode("utf-8")))
    return result


# ---------------------------------------------------------------------- get
@dataclass
class Selector:
    lines: tuple[int, int] | None = None
    bytes: tuple[int, int] | None = None
    records: tuple[int, int] | None = None
    json_pointer: str | None = None
    symbol: str | None = None  # Python: dotted def/class name via stdlib ast


def _parse(ref_text: str) -> Ref:
    return parse_ref(ref_text)


def _route_workspace(store: Store, ws: Workspace, ref: Ref) -> tuple[Store, Workspace]:
    """Resolve a ws:<alias>/ reference to its target workspace and store.

    A reference carrying an alias must never silently execute against the
    current workspace (wrong-repository evidence). Unknown aliases are
    rejected with the fix, not guessed (SPEC §5.1, §15).
    """
    if ref.workspace_alias is None:
        return store, ws
    from pathlib import Path as _Path

    from ctx.workspace import resolve_workspace

    path = ws.config.aliases.get(ref.workspace_alias)
    if path is None:
        known = ", ".join(sorted(ws.config.aliases)) or "none configured"
        raise RetrievalError(
            f"unknown workspace alias {ref.workspace_alias!r} (known: {known}); "
            "define it under [aliases] in ctx.toml or pass --workspace"
        )
    target_path = _Path(path)
    if not target_path.is_absolute():
        target_path = ws.root / target_path
    target = resolve_workspace(str(target_path))
    return Store(target.workspace_id), target


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
    store, ws = _route_workspace(store, ws, ref)
    budget = ws.config.budgets

    label: str
    data: bytes
    divergence: str | None = None

    fast_lines: tuple[str, int] | None = None  # (blob_hash, total_lines)
    fast_lines_raw = 0

    if ref.kind == "run":
        manifest = store.get_manifest(ref.id or "")
        short = str(manifest["id"]).removeprefix("sha256:")[:12]
        stream = ref.stream or "stdout"
        meta = manifest["streams"].get(stream)
        if meta is None:
            raise RetrievalError(f"run:{short} has no stream {stream!r}")
        blob_hash = str(meta["blob"]).removeprefix("sha256:")
        label = f"run:{short}#{stream}"
        if (
            selector.lines is not None
            and selector.json_pointer is None
            and not str(meta["mediaType"]).startswith("application/octet-stream")
        ):
            # Line-index fast path: touch only the requested byte range.
            fast_lines = (blob_hash, int(meta["lines"]))
            fast_lines_raw = int(meta["bytes"])
            data = b""
        else:
            data = store.get_blob(blob_hash)
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

    if selector.symbol is not None:
        if fast_lines is not None:
            data = store.get_blob(fast_lines[0])
            fast_lines = None
        span = _python_symbol_span(data.decode("utf-8", "replace"), selector.symbol)
        if span is None:
            raise RetrievalError(
                f"symbol {selector.symbol!r} not found (Python ast parser; "
                "for other languages use ctx search + --lines)"
            )
        header.append(f"symbol: {selector.symbol} → lines {span[0]}:{span[1]}")
        selector = Selector(lines=span)

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
        if fast_lines is not None:
            blob_hash, total = fast_lines
            a, b = selector.lines  # type: ignore[misc]
            b = min(b, total)
            if b - a + 1 > budget.max_inline_lines:
                b = a + budget.max_inline_lines - 1
                continuation = f"ctx get {ref_text} --lines {b + 1}:{min(total, b + budget.max_inline_lines)}"
            chunk = store.read_blob_lines(blob_hash, a, b)
            all_lines = chunk.decode("utf-8", "replace").splitlines()
            header.append(f"selector: --lines {a}:{b} of {fmt_int(total)}")
            body = "\n".join(f"L{a + i}: {ln}" for i, ln in enumerate(all_lines))
        else:
            if b"\x00" in data[:8192]:
                raise RetrievalError("binary content: use --bytes A:B for exact slices")
            all_lines = data.decode("utf-8", "replace").splitlines()
            if selector.lines is None:
                a, b = 1, min(len(all_lines), ws.config.budgets.max_inline_lines)
            else:
                a, b = selector.lines
            b = min(b, len(all_lines))
            if b - a + 1 > budget.max_inline_lines:
                b = a + budget.max_inline_lines - 1
                continuation = f"ctx get {ref_text} --lines {b + 1}:{min(len(all_lines), b + budget.max_inline_lines)}"
            header.append(f"selector: --lines {a}:{b} of {fmt_int(len(all_lines))}")
            body = "\n".join(f"L{n}: {all_lines[n - 1]}" for n in range(max(1, a), b + 1))

    if divergence:
        header.append(f"divergence: {divergence}")
    result = _emit(ws, "\n".join(header) + "\n" + body, budget.result_tokens, continuation)
    raw_len = fast_lines_raw if fast_lines is not None else len(data)
    record_telemetry(store, "get", raw_len, len(result.encode("utf-8")))
    return result


def _python_symbol_span(source: str, dotted: str) -> tuple[int, int] | None:
    """Line span of a def/class by dotted-name suffix using stdlib ast.
    Matches ``func``, ``Class.method``, or ``module-level path`` suffixes."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    want = dotted.split(".")

    def walk(node: Any, path: list[str]) -> tuple[int, int] | None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_path = path + [child.name]
                if new_path[-len(want) :] == want:
                    end = getattr(child, "end_lineno", child.lineno)
                    # Include decorators in the span.
                    start = min(
                        [child.lineno] + [d.lineno for d in child.decorator_list]
                    )
                    return (start, end)
                found = walk(child, new_path)
                if found:
                    return found
        return None

    return walk(tree, [])


# ---------------------------------------------------------------- telemetry
def record_telemetry(store: Store, op: str, raw_bytes: int, emitted_bytes: int) -> None:
    """Append an operational telemetry event. Kept strictly outside stable
    digests (SPEC §17); failures are swallowed — telemetry must never block."""
    import json as _json
    import time as _time

    try:
        path = store.audit_dir / "telemetry.jsonl"
        event = {
            "ts": _time.time(),
            "op": op,
            "raw_bytes": raw_bytes,
            "emitted_bytes": emitted_bytes,
            "est_tokens_avoided": max(0, (raw_bytes - emitted_bytes) // 4),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass


def telemetry_summary(store: Store) -> dict[str, int]:
    import json as _json

    totals = {"events": 0, "raw_bytes": 0, "emitted_bytes": 0, "est_tokens_avoided": 0}
    path = store.audit_dir / "telemetry.jsonl"
    if not path.is_file():
        return totals
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ev = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            totals["events"] += 1
            for key in ("raw_bytes", "emitted_bytes", "est_tokens_avoided"):
                totals[key] += int(ev.get(key, 0))
    except OSError:
        pass
    return totals


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
    store, ws = _route_workspace(store, ws, ref)
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
