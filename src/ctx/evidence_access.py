"""Scoped evidence access for plans and SDK consumers, independent of policies."""
from __future__ import annotations

from dataclasses import dataclass, field

from ctx.refs import parse_ref
from ctx.execution import snapshot_file
from ctx.semantic.evidence import _source
from ctx.semantic.contract import SemanticError


def within(path, scopes):
    return any(s == "." or path == s or path.startswith(s + "/") for s in scopes)


@dataclass
class EvidenceAccess:
    scopes: tuple[str, ...] = (".",)
    allowed_refs: set[str] = field(default_factory=set)
    source_bytes: int = 4 * 1024 * 1024
    read_bytes: int = 16000
    max_files: int = 10000
    probes: dict[str, list[str]] = field(default_factory=dict)
    worker_spec: dict | None = None

    def check(self, ws, ref):
        parsed = parse_ref(ref)
        if parsed.workspace_alias:
            raise SemanticError("cross-workspace evidence is outside this execution context")
        if parsed.kind == "repo":
            path = ws.confine(parsed.path or ".", must_exist=True)
            rel = ws.relativize(path)
            if not within(rel or ".", self.scopes) or ws.is_ignored(rel):
                raise SemanticError("evidence is outside the declared scope")
        elif ref not in self.allowed_refs:
            raise SemanticError("evidence handle has not been admitted to this context")
        return parsed

    def files(self, ws):
        names = set()
        for scope in self.scopes:
            names.update(ws.list_files(scope))
            if len(names) > self.max_files:
                raise SemanticError("discovery exceeds max_files; narrow the scope")
        return sorted(names)

    def read(self, ws, store, ref, start, end):
        parsed = self.check(ws, ref)
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end:
            raise SemanticError("read needs an inclusive positive line range")
        snapshot = None
        frozen = ref
        if parsed.kind == "repo":
            snapshot = snapshot_file(store, ws, parsed.path, max_bytes=self.source_bytes)
            frozen = "snapshot:" + snapshot["id"].removeprefix("sha256:")
        raw, view, redactions = _source(ws, store, frozen, self.source_bytes)
        raw_ref, view_ref = "blob:" + store.put_blob(raw), "blob:" + store.put_blob(view)
        lines = view.count(b"\n") + int(bool(view) and not view.endswith(b"\n"))
        if end > lines:
            raise SemanticError(f"read ends beyond the {lines} available lines")
        selected = store.read_blob_lines(view_ref[5:], start, end)
        if len(selected) > self.read_bytes:
            raise SemanticError("read exceeds read_bytes; narrow the range")
        self.allowed_refs.add(view_ref)
        result = {"ref": view_ref, "source": ref, "raw_ref": raw_ref, "start": start, "end": end,
                  "text": selected.decode(), "redactions": redactions, "transformed": raw != view}
        if snapshot is not None:
            result["snapshot"] = "snapshot:" + snapshot["id"].removeprefix("sha256:")
            result["path"] = parsed.path
            # Snapshot edits refer to raw coordinates. Redacted views cannot
            # confer authority to replace bytes the model never observed.
            if raw == view:
                result["span"] = f"{start}:{end}"
        return result
