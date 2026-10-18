"""Snapshot-backed progressive reads and explicit, epoch-scoped delivery memory.

Only complete emitted lines earn reuse. Matching is by coordinate and content,
so insertions may re-emit unchanged code but never hide changed code. Hosts must
advance the epoch when context is compacted, discarded, or restored differently.
"""
from __future__ import annotations

import hashlib

from ctx import anchors
from ctx.execution import snapshot_file
from ctx.retrieval import RetrievalError
from ctx.textutil import sanitize_for_model
from ctx.toolresult import fit_model_view

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_REMEMBERED_LINES = 8192


def read_view(ws, store, path, start, end, memory, *, fresh=False, snapshot=None):
    snap = snapshot or snapshot_file(store, ws, path, max_bytes=MAX_SOURCE_BYTES)
    ref = "snapshot:" + snap["id"].removeprefix("sha256:")
    raw = store.get_blob(snap["blob"].removeprefix("sha256:"))
    if b"\x00" in raw[:8192]:
        raise RetrievalError("binary source: use get with a byte selector")
    original = raw.decode("utf-8", "replace").splitlines()
    view, redactions = sanitize_for_model(raw.decode("utf-8", "replace"), ws.config.redaction)
    lines = view.splitlines()
    # A redaction may span lines. The normal retrieval path handles that view;
    # it cannot safely participate in coordinate-based delivery reuse.
    if len(lines) != len(original):
        from ctx.retrieval import get, Selector
        return get(store, ws, ref, Selector(lines=(start, end))), [ref]
    if not lines:
        return f"[ctx read repo:{snap['path']} · empty]\nsnapshot: {ref}", [ref]
    if start > len(lines):
        raise RetrievalError(f"read starts beyond {len(lines)} available lines")
    end = min(end, len(lines))
    path = snap["path"]
    entry = memory.setdefault(path, {"snapshot": ref, "lines": {}})
    previous = entry["lines"]
    fingerprints = {n: hashlib.sha256((original[n-1] + "\x00" + lines[n-1]).encode()).hexdigest()
                    for n in range(start, end + 1)}
    needed = [n for n in fingerprints if fresh or previous.get(str(n)) != fingerprints[n]]
    reused = end - start + 1 - len(needed)
    cap = max(512, ws.config.budgets.result_tokens * 4)
    head = f"[ctx read repo:{path} L{start}:{end} of {len(lines)}]\nsnapshot: {ref}\n"
    if len(head.encode()) > cap // 2:
        head = f"[ctx read L{start}:{end} of {len(lines)}; path omitted ({len(path.encode())} bytes)]\nsnapshot: {ref}\n"
    if redactions:
        head += "redactions: " + ",".join(redactions) + "\n"
    shown, body, content_bytes = [], [], 0

    def footer(count, next_line=None):
        omitted = len(needed) - count
        text = f"\nreused: {reused} lines in current context epoch; omitted: {omitted} unseen lines"
        if next_line is not None:
            text += f"\nnext: ctx get {ref} --lines {next_line}:{end}"
        if reused:
            text += f"\nfull: ctx get {ref} --lines {start}:{end} (or fresh=true)"
        return text

    for n in needed:
        row = f"L{n}: {lines[n-1]}"
        # Reserve for a live edit anchor as well as the omission footer.
        candidate = head + "\n".join([*body, row]) + footer(len(shown)+1, n+1) + "\nanchor: " + "x" * 32
        if (len(candidate.encode()) > cap or len(shown) >= ws.config.budgets.max_inline_lines
                or content_bytes + len(lines[n-1].encode()) > ws.config.budgets.max_inline_bytes):
            break
        shown.append(n)
        body.append(row)
        content_bytes += len(lines[n-1].encode())
    # A single minified line still gets a bounded preview, but no reuse credit.
    if needed and not shown:
        n = needed[0]
        from ctx.retrieval import get, Selector
        return get(store, ws, ref, Selector(lines=(n, end))), [ref]
    tail = footer(len(shown), needed[len(shown)] if len(shown) < len(needed) else None)
    # One anchor for a contiguous complete read; snapshot+lines also supports
    # edits when the delta consists of disjoint spans.
    if shown and shown == list(range(start, end + 1)) and not redactions:
        tail += f"\nanchor: {start}:{end}@{anchors.anchor(original[start-1:end])}"
    for n in shown:
        previous[str(n)] = fingerprints[n]
    entry["snapshot"] = ref
    # Eviction only causes future re-emission, never an omission.
    while sum(len(e["lines"]) for e in memory.values()) > MAX_REMEMBERED_LINES:
        memory.pop(next(iter(memory)))
    return head + "\n".join(body) + tail, [ref]


def navigate(ws, store, path, memory, *, symbol=None, start=None, end=None, mode="auto", fresh=False, snapshot=None):
    """Choose a small direct read, an indexed outline, or one symbol body."""
    snap = snapshot or snapshot_file(store, ws, path, max_bytes=MAX_SOURCE_BYTES)
    ref = "snapshot:" + snap["id"].removeprefix("sha256:")
    if start is not None:
        return read_view(ws, store, path, start, end, memory, fresh=fresh, snapshot=snap)
    if not symbol and mode == "auto" and snap["lines"] <= min(200, ws.config.budgets.max_inline_lines):
        return read_view(ws, store, path, 1, snap["lines"], memory, fresh=fresh, snapshot=snap)
    from ctx.skeleton import skeleton_for, skeleton_outline
    skeleton = skeleton_for(store, ws, path)
    if skeleton["blob"] != snap["blob"]:
        raise RetrievalError("source changed during navigation; retry the read")
    if symbol:
        matches = [s for s in skeleton["symbols"] if s["name"] == symbol or
                   (str(s.get("scope") or "") + "." + s["name"]).strip(".") == symbol]
        if len(matches) != 1:
            raise RetrievalError("symbol absent or ambiguous; request an outline or exact line range")
        a, b = matches[0]["range"]
        return read_view(ws, store, path, a, b, memory, fresh=fresh, snapshot=snap)
    outline = skeleton_outline(skeleton, max(64, ws.config.budgets.result_tokens - 64))
    outline, _ = sanitize_for_model(outline, ws.config.redaction)
    return fit_model_view(f"snapshot: {ref}\n", outline, ref, ws.config.budgets.result_tokens * 4), [ref]
