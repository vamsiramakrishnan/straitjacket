"""Epoch checkpoints (SPEC §14).

A checkpoint freezes task state — goal, decisions, hypotheses, evidence
handles with exact coordinates, attempted searches, and file status — into a
content-addressed manifest whose referenced artifacts are pinned against
garbage collection. A new conversation (cache epoch) starts from the
rendered document; the old transcript and ledger stay immutable.
"""

from __future__ import annotations

from typing import Any

from ctx.refs import RefError, parse_ref
from ctx.store import Store
from ctx.textutil import sanitize_for_model
from ctx.workspace import Workspace


def create_checkpoint(
    store: Store,
    ws: Workspace,
    *,
    goal: str,
    state: str | None = None,
    decisions: list[str] | None = None,
    hypotheses: list[str] | None = None,
    evidence: list[str] | None = None,
    attempted: list[str] | None = None,
    files: list[str] | None = None,
) -> tuple[str, str]:
    """Returns (checkpoint_id, rendered document)."""
    resolved_evidence: list[dict[str, str]] = []
    for item in evidence or []:
        # "run:abc123#stdout L100:120  note text" — first token is the ref.
        parts = item.split(None, 1)
        if not parts:
            continue  # blank/whitespace-only evidence line: skip, don't crash
        ref_text = parts[0]
        note = parts[1] if len(parts) > 1 else ""
        try:
            ref = parse_ref(ref_text)
        except RefError as e:
            raise RefError(f"checkpoint evidence {item!r}: {e}") from e
        if ref.id:
            full = store.resolve_id(ref.id)
            store.pin(full)  # survives gc for the life of the checkpoint
            ref_text = ref_text.replace(ref.id, full[:12], 1)
        resolved_evidence.append({"ref": ref_text, "note": note})

    manifest: dict[str, Any] = {
        "schema": "ctx.checkpoint/v1",
        "workspaceId": ws.workspace_id,
        "goal": goal,
        "state": state or "",
        "decisions": list(decisions or []),
        "hypotheses": list(hypotheses or []),
        "evidence": resolved_evidence,
        "attempted": list(attempted or []),
        "files": list(files or []),
        "source": {"gitHead": ws.git.head if ws.git else None},
    }
    cp_id = store.put_manifest(manifest, kind="checkpoint")
    store.pin(cp_id)
    return cp_id, render_checkpoint(ws, cp_id, manifest)


def render_checkpoint(ws: Workspace, cp_id: str, manifest: dict[str, Any]) -> str:
    lines = [f"[ctx checkpoint:{cp_id[:12]}]"]
    lines.append(f"goal: {manifest['goal']}")
    if manifest.get("state"):
        lines.append(f"state: {manifest['state']}")
    for title, key in (
        ("decisions", "decisions"),
        ("open hypotheses", "hypotheses"),
        ("attempted (incl. negative searches)", "attempted"),
        ("files changed / verification", "files"),
    ):
        items = manifest.get(key) or []
        if items:
            lines.append(f"{title}:")
            lines.extend(f"  - {item}" for item in items)
    ev = manifest.get("evidence") or []
    if ev:
        lines.append("evidence (pinned):")
        for e in ev:
            note = f"  — {e['note']}" if e.get("note") else ""
            lines.append(f"  - {e['ref']}{note}")
    head = (manifest.get("source") or {}).get("gitHead")
    if head:
        lines.append(f"git: HEAD {head[:12]}")
    lines.append(
        "resume: start a new conversation from this document; "
        "retrieve evidence with ctx get/search on the handles above."
    )
    text, _ = sanitize_for_model("\n".join(lines), ws.config.redaction.patterns)
    return text


def show_checkpoint(store: Store, ws: Workspace, cp_ref: str) -> str:
    ref = parse_ref(cp_ref)
    if ref.kind != "checkpoint":
        raise RefError(f"expected checkpoint:<id>, got {cp_ref!r}")
    manifest = store.get_manifest(ref.id or "")
    return render_checkpoint(ws, str(manifest["id"]).removeprefix("sha256:"), manifest)
