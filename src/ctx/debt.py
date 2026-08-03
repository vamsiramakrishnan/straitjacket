"""`ctx debt`: declared omission for engineering decisions.

SPEC §8's rule for bytes — nothing is dropped silently; every omission
carries coordinates — applied to scope. A deferred improvement, a
deliberately-not-built feature, a known-suboptimal shortcut: each becomes
an addressable ledger entry instead of a vanished thought. The ledger is
committed workspace state (like ctx-policy.toml), so debt survives
sessions and travels with the repo.

Entries are append-only JSONL; `resolve` marks rather than deletes, so the
history of judgment is preserved (immutability principle, applied gently).
"""

from __future__ import annotations

import json
from pathlib import Path

DEBT_FILENAME = ".ctx-debt.jsonl"


def _path(workspace_root: Path) -> Path:
    return Path(workspace_root) / DEBT_FILENAME


def _load(workspace_root: Path) -> list[dict]:
    entries: list[dict] = []
    p = _path(workspace_root)
    if not p.is_file():
        return entries
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            entries.append(doc)
    return entries


def add(workspace_root: Path, note: str, *, ref: str = "") -> str:
    """Record a deferred decision. Returns the entry id (content-derived:
    the same declaration is the same debt — idempotent by construction)."""
    import hashlib

    note = " ".join(note.split())
    if not note:
        raise ValueError("debt note must not be empty")
    # Length-prefixed, not delimiter-joined: `f"{note}|{ref}"` lets two
    # distinct declarations straddle the separator differently and hash to
    # the same id (note="a|b", ref="" collides with note="a", ref="b"), so
    # the second silently becomes an update to the first.
    _basis = f"{len(note)}:{note}{len(ref)}:{ref}"
    entry_id = hashlib.sha256(_basis.encode("utf-8")).hexdigest()[:10]
    entries = _load(workspace_root)
    if any(e.get("id") == entry_id and e.get("op") == "add" for e in entries):
        return entry_id  # already declared; idempotent
    with _path(workspace_root).open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"op": "add", "id": entry_id, "note": note, "ref": ref},
                sort_keys=True,
            )
            + "\n"
        )
    return entry_id


def resolve(workspace_root: Path, entry_id: str) -> bool:
    """Mark a debt entry resolved. Returns False for an unknown id."""
    entries = _load(workspace_root)
    known = {e.get("id") for e in entries if e.get("op") == "add"}
    if entry_id not in known:
        return False
    with _path(workspace_root).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"op": "resolve", "id": entry_id}, sort_keys=True) + "\n")
    return True


def outstanding(workspace_root: Path) -> list[dict]:
    """Open debt, in declaration order."""
    entries = _load(workspace_root)
    resolved = {e.get("id") for e in entries if e.get("op") == "resolve"}
    return [
        e for e in entries if e.get("op") == "add" and e.get("id") not in resolved
    ]


def render(workspace_root: Path) -> str:
    open_entries = outstanding(workspace_root)
    all_entries = _load(workspace_root)
    n_resolved = len({e.get("id") for e in all_entries if e.get("op") == "resolve"})
    lines = [f"[ctx debt · {len(open_entries)} open · {n_resolved} resolved]"]
    for e in open_entries:
        ref = f"  ({e['ref']})" if e.get("ref") else ""
        lines.append(f"  {e['id']}  {e['note']}{ref}")
    if not open_entries:
        lines.append("  (no outstanding declared debt)")
    return "\n".join(lines)
