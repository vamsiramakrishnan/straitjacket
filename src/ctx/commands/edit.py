"""The addressable edit transaction CLI: plan, preview, apply."""

from __future__ import annotations

import sys


def cmd_edit(ws, ns) -> int:
    import json

    from ctx.edit_transactions import (
        EditTransactionError,
        apply_edit_plan,
        create_edit_plan,
        load_json,
        preview_edit_plan,
        write_json,
    )
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        source = ws.confine(ns.file, must_exist=True)
        value = load_json(source)
        if ns.edit_cmd == "plan":
            result = create_edit_plan(ws, store, value)
            destination = ws.confine(ns.out)
            write_json(destination, result)
            print(
                f"[ctx edit · planned] {len(result['edits'])} edit(s) · "
                f"plan {result['id']} · wrote {ws.relativize_as_asked(ns.out)}"
            )
            return 0
        if ns.edit_cmd == "preview":
            result = preview_edit_plan(ws, store, value)
        else:
            result = apply_edit_plan(ws, value)
        if ns.receipt:
            write_json(ws.confine(ns.receipt), result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except EditTransactionError as e:
        receipt_path = getattr(ns, "receipt", None)
        if receipt_path and e.receipt is not None:
            write_json(ws.confine(receipt_path), e.receipt)
        print(f"ctx edit: {e}", file=sys.stderr)
        return 2


__all__ = ["cmd_edit"]
