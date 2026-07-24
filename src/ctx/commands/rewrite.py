"""The edit-loop collapse: `ctx rewrite`."""

from __future__ import annotations

import sys


def cmd_rewrite(ws, ns) -> int:
    """Structural multi-file rewrite in one bounded op — the edit-loop collapse.

    One call replaces the search-read-edit-per-file loop: ast-grep computes the
    mechanical rewrite across every matching file, mints the unified diff as an
    addressable ``blob:``, and (with ``--apply``) applies it transactionally
    (generation-guarded git apply, all-or-nothing). Preview by default."""
    from ctx import astgrep
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        rows, meta = astgrep.rewrite_preview(
            ws, store, ns.pattern, ns.replacement,
            language=ns.lang, glob=ns.glob)
    except Exception as e:  # EngineMissing / RewriteError / parse failure
        print(f"ctx rewrite: {e}", file=sys.stderr)
        return 2
    blob = meta.get("patch_blob")
    total = sum(int(r.get("edits", 0)) for r in rows)
    print(f"[ctx rewrite · preview] {meta.get('files', 0)} file(s), "
          f"{total} edit(s) · {meta.get('precision', 'structural')} · {meta.get('engine')}")
    for r in rows:
        print(f"  {r['file']}: {r.get('edits', 0)} edit(s)")
    if not blob:
        print("no matches — nothing to rewrite")
        return 0
    print(f"patch: {blob}   (ctx get {blob}  for the unified diff)")
    if not ns.apply:
        print("dry run — add --apply to write the change transactionally")
        return 0
    try:
        applied, ameta = astgrep.rewrite_apply(ws, store, blob, meta.get("generation"))
    except Exception as e:
        print(f"ctx rewrite: apply refused — {e}", file=sys.stderr)
        return 2
    print(f"[ctx rewrite · applied] {ameta.get('applied_files', len(applied))} file(s) "
          f"in one transactional op")
    return 0
