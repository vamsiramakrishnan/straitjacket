"""``ctx get``: exact bounded slice with provenance (SPEC §6.4). Oversized
requests return a bounded preview plus continuation coordinates — never
silent flooding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ctx.execution import snapshot_file
from ctx.store import Store
from ctx.textutil import estimate_tokens, fmt_int
from ctx.workspace import Workspace

from .common import RetrievalError, _emit, _parse, _peek_blob, _read_bytes_range, _route_workspace
from .spans import _resolve_span
from .telemetry import record_telemetry


@dataclass(frozen=True, slots=True)
class Selector:
    lines: tuple[int, int] | None = None
    bytes: tuple[int, int] | None = None
    records: tuple[int, int] | None = None
    json_pointer: str | None = None
    symbol: str | None = None  # Python: dotted def/class name via stdlib ast
    span: str | None = None  # opaque span token minted by a digest (SPEC §6.4)


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
    fast_bytes: tuple[str, int] | None = None  # (blob_hash, total_bytes)

    # A selector combination that will end up needing the FULL decoded blob
    # regardless of ref kind: json-pointer (JSON parse) and records (must
    # scan every line to index non-blank records). Line-index/byte-range
    # fast paths below are only ever eligible when neither is requested —
    # this mirrors the priority the render dispatch below already uses
    # (json_pointer > records > bytes > lines), so no selector combination
    # changes behavior, only which path avoids a redundant full read.
    needs_full_scan = selector.json_pointer is not None or selector.records is not None

    if ref.kind == "run":
        manifest = store.get_manifest(ref.id or "")
        short = str(manifest["id"]).removeprefix("sha256:")[:12]
        stream = ref.stream or "stdout"
        meta = manifest["streams"].get(stream)
        if meta is None:
            raise RetrievalError(f"run:{short} has no stream {stream!r}")
        blob_hash = str(meta["blob"]).removeprefix("sha256:")
        label = f"run:{short}#{stream}"
        is_octet_stream = str(meta["mediaType"]).startswith("application/octet-stream")
        if selector.span is not None:
            data = b""  # span resolution reads only what it needs
        elif not needs_full_scan and selector.bytes is not None:
            # Byte-range selector: seek instead of loading the whole stream.
            fast_bytes = (blob_hash, int(meta["bytes"]))
            data = b""
        elif not needs_full_scan and not is_octet_stream:
            # Line-index fast path: touch only the requested byte range.
            # Covers both an explicit --lines and the implicit default view
            # (head lines) — both end up in the same "lines" render branch
            # below, so both are eligible.
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
            # (Needs the full blob either way, to diff byte-for-byte against
            # the live file — no fast path applies here.)
            try:
                current = ws.confine(str(manifest["path"])).read_bytes()
                if current != data:
                    divergence = "current worktree file differs from this snapshot"
            except Exception:
                divergence = "file no longer present in worktree"
        else:
            blob_id = store.resolve_id(ref.id or "", kinds=("blob",))
            label = f"blob:{blob_id[:12]}"
            if selector.span is not None:
                data = b""
            elif not needs_full_scan and selector.bytes is not None:
                fast_bytes = (blob_id, store.blob_path(blob_id).stat().st_size)
                data = b""
            elif (
                not needs_full_scan
                and selector.symbol is None
                and b"\x00" not in _peek_blob(store, blob_id)
            ):
                idx = store.line_index(blob_id)
                fast_lines = (blob_id, max(0, len(idx) - 1))
                fast_lines_raw = int(idx[-1]) if idx else 0
                data = b""
            else:
                data = store.get_blob(blob_id)
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

    if selector.span is not None:
        result = _resolve_span(store, ws, ref_text, label, selector.span)
        record_telemetry(store, "get", len(data) if data else 0, len(result.encode("utf-8")))
        return result

    if selector.symbol is not None:
        if fast_lines is not None:
            data = store.get_blob(fast_lines[0])
            fast_lines = None
        if fast_bytes is not None:
            data = store.get_blob(fast_bytes[0])
            fast_bytes = None
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
            from ctx.textutil import loads_fast

            doc = loads_fast(data.decode("utf-8", "replace"))
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
        if fast_bytes is not None:
            blob_hash_b, total_b = fast_bytes
            chunk = _read_bytes_range(store, blob_hash_b, a, min(b, total_b))
            header.append(f"selector: --bytes {a}:{b} of {fmt_int(total_b)}")
            body = chunk.decode("utf-8", "replace")
            if b < total_b:
                continuation = f"ctx get {ref_text} --bytes {b + 1}:{min(total_b, b + (b - a + 1))}"
        else:
            chunk = data[a - 1 : b]
            header.append(f"selector: --bytes {a}:{b} of {fmt_int(len(data))}")
            body = chunk.decode("utf-8", "replace")
            if b < len(data):
                continuation = f"ctx get {ref_text} --bytes {b + 1}:{min(len(data), b + (b - a + 1))}"
    else:
        if fast_lines is not None:
            blob_hash, total = fast_lines
            a, b = selector.lines if selector.lines is not None else (1, total)
            if selector.lines is None:
                b = min(total, ws.config.budgets.max_inline_lines)
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
    if fast_lines is not None:
        raw_len = fast_lines_raw
    elif fast_bytes is not None:
        raw_len = fast_bytes[1]
    else:
        raw_len = len(data)
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
