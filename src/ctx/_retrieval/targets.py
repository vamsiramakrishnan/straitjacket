"""Search targets: the searchable units addressed by ``ctx search`` (run
streams, blobs, and repo files) and how each kind is resolved."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from ctx.refs import Ref
from ctx.store import Store
from ctx.workspace import Workspace

from .common import RetrievalError, _peek_blob


def _glob_match(rel: str, glob: str) -> bool:
    """fnmatch with the convention that ``**/x`` also matches top-level ``x``."""
    if fnmatch.fnmatch(rel, glob):
        return True
    return glob.startswith("**/") and fnmatch.fnmatch(rel, glob[3:])


def _stream_text(store: Store, blob_ref: str) -> str:
    blob_hash = str(blob_ref).removeprefix("sha256:")
    # Bounded memory (task 3): sniff only the first 8 KiB before deciding to
    # load the rest — a huge binary blob must not be fully read into memory
    # just to be rejected. See the perf-pass benchmark in the report.
    if b"\x00" in _peek_blob(store, blob_hash):
        raise RetrievalError("binary stream: use --bytes selectors on blob content")
    return store.get_blob(blob_hash).decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
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


def _resolve_run_targets(store: Store, ref: Ref) -> tuple[list[SearchTarget], int]:
    """Returns (targets, streams_skipped_binary).

    Debt 135d7df383 (S6 bug-bash): a binary stream used to raise straight out
    of this function, aborting the whole multi-stream search even when the
    OTHER stream (e.g. a text stderr next to a binary stdout) had a perfectly
    searchable match. It is skipped instead, with a declared, counted note —
    the same contract ``_resolve_repo_targets`` already honors for binary
    files.
    """
    manifest = store.get_manifest(ref.id or "")
    short = str(manifest["id"]).removeprefix("sha256:")[:12]
    names = [ref.stream] if ref.stream else ["stdout", "stderr"]
    targets: list[SearchTarget] = []
    skipped_binary = 0
    for name in names:
        meta = manifest["streams"].get(name)
        if not meta or not meta["bytes"]:
            continue
        try:
            text = _stream_text(store, meta["blob"])
        except RetrievalError:
            skipped_binary += 1
            continue
        targets.append(SearchTarget(label=f"run:{short}#{name}", text=text))
    return targets, skipped_binary


def _resolve_repo_targets(
    store: Store,
    ws: Workspace,
    ref: Ref,
    *,
    glob: str | None,
    scope: str | None,
    max_files: int = 5000,
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
    # The session ledger is bookkeeping, never evidence (hook.py rule; the
    # q search stage and generation hashing exclude it likewise). It also
    # grows as the harness runs, so including it makes repo search observe
    # its own state — the byte-stability failure mode, engine-independent.
    rels = [r for r in rels if r.replace("\\", "/").split("/")[0] != ".ctx-session-reads"]
    if glob:
        rels = [r for r in rels if _glob_match(r, glob)]
    rels = rels[:max_files]

    targets: list[SearchTarget] = []
    skipped_binary = 0
    # Named distinctly from the ``root`` loop variable above (str | None,
    # one per configured scope root) — reusing the name there confused
    # mypy's type-flow into a spurious str|None/Path union (part of debt
    # bf48ba3c4e's mypy residual, closed out by this split).
    ws_root = ws.root
    for rel in rels:
        # rels come from ws.list_files (already confined + ignore-filtered);
        # skip per-file re-confinement syscalls on the hot loop.
        try:
            data = (ws_root / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            skipped_binary += 1
            continue
        targets.append(SearchTarget(label=rel, text=data.decode("utf-8", "replace")))
    return targets, len(rels), skipped_binary
