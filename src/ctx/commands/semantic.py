"""Explicit semantic execution; kept off the deterministic observation paths."""
from __future__ import annotations

import sys


def cmd_semantic(ws, ns) -> int:
    from ctx import semantic
    from ctx.retrieval import _emit
    from ctx.semantic.contract import parse_json
    from ctx.semantic.evidence import read_document
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        if ns.semantic_cmd == "prepare":
            if ns.request_file == "-":
                data = sys.stdin.buffer.read(128001)
            else:
                with ws.confine(ns.request_file, must_exist=True).open("rb") as fh:
                    data = fh.read(128001)
            if len(data) > 128000:
                raise semantic.SemanticError("request exceeds 128000 bytes")
            handle = semantic.prepare(ws, store, parse_json(data))
            plan = read_document(store, handle)
            limits, worker = plan["spec"]["limits"], plan["spec"]["worker"]
            next_step = (f"run: ctx semantic run {handle}" if worker.get("command")
                         else "run: supply worker=callback through the semantic SDK")
            text = (f"[ctx semantic · prepared · no model invoked]\n{handle}\n"
                    f"{len(plan['sources'])} sources · {len(plan['partitions'])} partitions\n"
                    f"worker: {worker['model']} ({worker['identity']})\n"
                    f"limits: {limits['max_calls']} worker calls · {limits['wall_seconds']} seconds · "
                    f"USD {limits['max_cost_usd']} / {limits['max_tokens']} tokens admission\n"
                    f"{next_step}")
            print(_emit(ws, text, ws.config.budgets.result_tokens, handle=handle))
            return 0
        action = semantic.inspect if ns.semantic_cmd == "show" else semantic.run
        kwargs = {} if ns.semantic_cmd == "show" else {"retry_failed": ns.retry_failed}
        handle, report = action(ws, store, ns.handle, **kwargs)
        coverage, totals = report["coverage"], report["totals"]
        rows = [f"[ctx semantic · {report['status']} · model inference]",
                f"report: {handle}",
                f"coverage: {coverage['completed_partitions']}/{coverage['selected_partitions']} partitions; "
                f"{coverage['processed_bytes']}/{coverage['selected_bytes']} selected bytes",
                "selection completeness: unknown; citations checked for membership, not truth",
                f"calls: {totals['calls']} · known cost USD {totals['known_cost_usd']:.6f} · "
                f"unknown-cost calls: {totals['unknown_cost_calls']}"]
        if report["stop_reason"]:
            rows.append("stop: " + report["stop_reason"])
        rows.extend("- " + finding["summary"] for finding in report["findings"][:8])
        if len(report["findings"]) > 8:
            rows.append(f"omitted: {len(report['findings']) - 8} more findings in {handle}")
        rows.append(f"unresolved dependencies: {len(report['unresolved'])}; inspect: ctx get {handle}")
        print(_emit(ws, "\n".join(rows), ws.config.budgets.result_tokens, handle=handle))
        return 0 if report["status"] == "complete" or ns.semantic_cmd == "show" else 1
    except semantic.SemanticError as exc:
        print(f"ctx semantic: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        handle = getattr(ns, "handle", None)
        print("ctx semantic: interrupted" + (f"; inspect or resume {handle}" if handle else ""), file=sys.stderr)
        return 130
    finally:
        store.close()
