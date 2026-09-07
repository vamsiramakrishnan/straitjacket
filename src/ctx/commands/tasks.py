"""CLI for durable tasks; execution services stay available independently in SDK."""
from __future__ import annotations

import sys


def cmd_execution_task(ws, ns):
    from ctx.controllers import investigation
    from ctx.retrieval import _emit
    from ctx.semantic.contract import parse_json
    from ctx.store import Store
    from ctx.task_runtime import TaskRuntime, ExecutionPaused
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        if ns.task_cmd == "prepare":
            if ns.request_file == "-":
                data = sys.stdin.buffer.read(128001)
            else:
                with ws.confine(ns.request_file, must_exist=True).open("rb") as f:
                    data = f.read(128001)
            if len(data) > 128000:
                raise ValueError("task request exceeds 128000 bytes")
            task = investigation.prepare(ws, store, parse_json(data))
            print(f"prepared: {task}\nrun: ctx task run {task}")
            return 0
        if ns.task_cmd == "cancel":
            TaskRuntime(ws, store, ns.task).cancel()
            print(f"cancellation requested: {ns.task}")
            return 0
        if ns.task_cmd == "apply":
            result = investigation.apply(ws, store, ns.task)
            print(f"applied: {result['patch']}")
            return 0
        if ns.task_cmd == "show":
            handle, report = investigation.inspect(ws, store, ns.task)
        else:
            if ns.task_cmd == "resume":
                # A deliberate resume clears a previous cancellation request;
                # failed/uncertain billable attempts still need --retry-failed.
                TaskRuntime(ws, store, ns.task).cancellation_path.unlink(missing_ok=True)
            handle, report = investigation.run(ws, store, ns.task,
                                               retry_failed=ns.retry_failed)
        totals = report["totals"]
        lines = [f"[ctx task {report['task_id']} · {report['status']}]",
                 f"phase: {report['phase']} · rounds: {report['rounds']} · repairs: {len(report['repairs'])}",
                 f"calls: {totals['calls']} · charged seconds: {totals['charged_seconds']:.2f}",
                 f"known cost: USD {totals['known_cost_usd']:.6f} · unknown-cost calls: {totals['unknown_cost_calls']}",
                 f"report: {handle}", f"worktree: {report['worktree']}"]
        if report["stop_reason"]:
            lines.append("stop: " + report["stop_reason"])
        if report["patch"]:
            lines.extend(["patch: " + report["patch"], "verification: " + report["verification"],
                          f"apply: ctx task apply {report['task_id']}"])
        print(_emit(ws, "\n".join(lines), ws.config.budgets.result_tokens, handle=handle))
        return 0 if ns.task_cmd == "show" or report["status"] == "verified" else 1
    except KeyboardInterrupt:
        print(f"interrupted; inspect with ctx task show {getattr(ns, 'task', '')}", file=sys.stderr)
        return 130
    except (ValueError, ExecutionPaused) as exc:
        print(f"ctx task: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
