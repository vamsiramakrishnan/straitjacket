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
        replace_span,
        write_json,
    )
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    from ctx.edit_verification import Check, VerificationError, verify_edit

    try:
        if ns.edit_cmd == "handoff":
            from ctx.prewalk import create_handoff
            state = load_json(ws.confine(ns.state, must_exist=True))
            result = create_handoff(ws, store, ns.verification, state)
            print(result["signal"])
            return 0
        if ns.edit_cmd == "verify":
            command = list(ns.command)
            if command and command[0] == "--":
                command.pop(0)
            result = verify_edit(ws, store, ns.ref,
                                 [Check(ns.kind, tuple(command), ns.timeout)], witnesses=ns.witness)
            if ns.receipt:
                write_json(ws.confine(ns.receipt), result)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 0 if result["outcome"] == "passed" else 3
        if ns.edit_cmd == "replace":
            replacement_path = ws.confine(ns.replacement_file, must_exist=True)
            if ws.is_ignored(ws.relativize(replacement_path)):
                raise EditTransactionError("replacement file excluded by policy")
            result = replace_span(ws, store, ns.ref, ns.lines,
                                  replacement_path.read_text(encoding="utf-8"), apply=ns.apply)
            if ns.receipt:
                write_json(ws.confine(ns.receipt), result)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 0
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
            try:
                result = apply_edit_plan(ws, value)
            except EditTransactionError as e:
                _record_anchored(ws, value, outcome=_refusal_outcome(str(e)))
                raise
            _record_anchored(ws, value, outcome="applied", receipt=result)
        if ns.receipt:
            write_json(ws.confine(ns.receipt), result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except VerificationError as e:
        print(f"ctx edit: {e}", file=sys.stderr)
        return 2
    except EditTransactionError as e:
        receipt_path = getattr(ns, "receipt", None)
        if receipt_path and e.receipt is not None:
            write_json(ws.confine(receipt_path), e.receipt)
        print(f"ctx edit: {e}", file=sys.stderr)
        return 2


def _refusal_outcome(reason: str) -> str:
    """Map an apply refusal onto the edit-outcome vocabulary.

    The anchored format has the same two addressable failures a needle has --
    the target moved or vanished (``not_found``) and the target now has more
    than one equally good copy (``not_unique``) -- and every other refusal
    (stale plan, overlap, a file that changed mid-commit) is ``other_error``.
    """
    text = reason.lower()
    if "changed or disappeared" in text:
        return "not_found"
    if "ambiguous" in text:
        return "not_unique"
    return "other_error"


def _record_anchored(ws, plan, *, outcome: str, receipt=None) -> None:
    """One ledger row per planned file, beside the host's own Edit/Write rows.

    Same ledger, same vocabulary, format ``anchored``: this is what lets a
    summary compare the anchored format against the host's native one for
    the model in use. Model comes from ``CTX_MODEL`` the same way the hook
    finds it. Fail-open like every telemetry write.
    """
    try:
        from ctx.edit_outcomes import append_edit_outcome, resolve_model

        edits = plan.get("edits") if isinstance(plan, dict) else None
        if not isinstance(edits, list):
            return
        by_path: dict[str, dict[str, int]] = {}
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            rel = str(edit.get("path") or "")
            sizes = by_path.setdefault(rel, {"old": 0, "new": 0})
            sizes["new"] += len(str(edit.get("replacement") or ""))
        # The bytes the plan replaced, per file, recovered from the receipt's
        # before/after sizes so the row's oldLen means what a native row's
        # does (the region the edit targeted, not the whole file). A refusal
        # has no receipt and records 0.
        replaced: dict[str, int] = {}
        if isinstance(receipt, dict):
            for item in receipt.get("files") or []:
                if isinstance(item, dict):
                    rel = str(item.get("path"))
                    delta = int(item.get("bytesBefore") or 0) - int(item.get("bytesAfter") or 0)
                    replaced[rel] = max(0, delta + by_path.get(rel, {}).get("new", 0))
        model = resolve_model()
        for rel, sizes in by_path.items():
            append_edit_outcome(
                ws.root,
                tool="ctx edit apply",
                outcome=outcome,
                path=rel or None,
                old_len=replaced.get(rel, 0),
                new_len=sizes["new"],
                flavor="ctx",
                model=model,
                fmt="anchored",
            )
    except Exception:
        return


__all__ = ["cmd_edit"]
