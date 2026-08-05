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
    # The patch covers EVERY matched file; the row list is capped. A preview
    # that lists 200 files and applies 210 is not a preview, so the gap is
    # declared where the reviewer is actually looking -- putting it only in
    # `meta` is not a declaration, it is a place to have put one.
    hidden = int(meta.get("preview_omitted", 0) or 0)
    if hidden:
        print(f"  … {hidden} more changed file(s) NOT listed above but INCLUDED "
              f"in the patch — review them before --apply "
              f"(ctx get {meta.get('patch_blob')} for the full diff)")
    if not blob:
        print("no matches — nothing to rewrite")
        return 0
    print(f"patch: {blob}   (ctx get {blob}  for the unified diff)")
    if not ns.apply:
        print("dry run — add --apply to write the change transactionally")
        return 0
    if hidden:
        # A bounded DISPLAY with a declared remainder is this project's house
        # style for a read. A mutation is different: "you reviewed 200, I
        # changed 210" is not something a note repairs, because the extra ten
        # are already written. The reviewable set and the applied set have to
        # be the same set, so this refuses rather than declares -- the same
        # posture the generation guard and the overlapping-rewrite check
        # already take in this module.
        print(
            f"ctx rewrite: apply refused — the patch changes {hidden} file(s) "
            f"beyond the {len(rows)} shown for review. Narrow --pattern or "
            f"--glob so the whole change is reviewable, then re-run.",
            file=sys.stderr,
        )
        return 2
    try:
        applied, ameta = astgrep.rewrite_apply(ws, store, blob, meta.get("generation"))
    except Exception as e:
        print(f"ctx rewrite: apply refused — {e}", file=sys.stderr)
        return 2
    print(f"[ctx rewrite · applied] {ameta.get('applied_files', len(applied))} file(s) "
          f"in one transactional op")
    return 0
