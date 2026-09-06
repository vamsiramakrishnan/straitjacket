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
    from ctx.astgrep import EngineMissing, RewriteError

    try:
        if ns.edit_cmd == "advise":
            from ctx.edit_policy import choose_format, load_rows
            decision = choose_format(load_rows(ws.confine(ns.file, must_exist=True)),
                                     model=ns.model, shape=ns.shape)
            print(json.dumps(decision, sort_keys=True, separators=(",", ":")))
            return 0
        if ns.edit_cmd == "expand":
            from ctx.edit_expansion import plan_expansion
            result = plan_expansion(ws, store, ns.verification, pattern=ns.pattern,
                                    replacement=ns.replacement, language=ns.lang, glob=ns.glob)
            if ns.receipt:
                write_json(ws.confine(ns.receipt), result)
            _emit_result(ws, store, result)
            return 0
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
            _emit_result(ws, store, result)
            return 0 if result["outcome"] == "passed" else 3
        if ns.edit_cmd == "replace":
            replacement_path = ws.confine(ns.replacement_file, must_exist=True)
            if ws.is_ignored(ws.relativize(replacement_path)):
                raise EditTransactionError("replacement file excluded by policy")
            result = replace_span(ws, store, ns.ref, ns.lines,
                                  replacement_path.read_text(encoding="utf-8"), apply=ns.apply)
            if ns.receipt:
                write_json(ws.confine(ns.receipt), result)
            _emit_result(ws, store, result)
            return 0
        if ns.edit_cmd in {"preview", "apply"} and ns.file.startswith("blob:"):
            from ctx.edit_verification import read_evidence
            value = read_evidence(store, ns.file, "ctx.edit-plan/v1")
        else:
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
        _emit_result(ws, store, result)
        return 0
    except (VerificationError, RewriteError, EngineMissing) as e:
        print(f"ctx edit: {e}", file=sys.stderr)
        return 2
    except EditTransactionError as e:
        receipt_path = getattr(ns, "receipt", None)
        if receipt_path and e.receipt is not None:
            write_json(ws.confine(receipt_path), e.receipt)
        print(f"ctx edit: {e}", file=sys.stderr)
        return 2
    finally:
        store.close()


__all__ = ["cmd_edit"]


def _emit_result(ws, store, result):
    """Preserve full receipts; bound only their model-visible projection."""
    import json
    from ctx.store import canonical_json
    from ctx.textutil import estimate_tokens, sanitize_for_model

    raw = canonical_json(result)
    text, _ = sanitize_for_model(raw.decode("utf-8"), ws.config.redaction)
    if estimate_tokens(len(text.encode("utf-8"))) > ws.config.budgets.result_tokens:
        ref = "blob:" + store.put_blob(raw)
        compact = {"outcome": result.get("outcome"), "receiptRef": ref,
                   "omittedBytes": len(raw), "files": len(result.get("files", [])),
                   "next": f"ctx get {ref}"}
        text = json.dumps(compact, sort_keys=True, separators=(",", ":"))
    print(text)
