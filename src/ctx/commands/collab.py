"""`ctx collab` — the multi-harness handoff surface over ctx.collab."""

from __future__ import annotations


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def cmd_collab(ws, ns) -> int:
    import json

    from ctx.collab import (
        CollabError,
        Ledger,
        handoff_packet,
        ledger_for_workspace,
        spawn,
    )

    ledger: Ledger = ledger_for_workspace(ws)
    action = ns.col_cmd

    def _die(message: str) -> int:
        print(f"collab: {message}")
        return 2

    if action == "new":
        task_id = ledger.create_task(
            ns.goal,
            owns=_split(ns.owns),
            avoid=_split(ns.avoid),
            verify=ns.verify,
            context=_split(ns.context),
            created_by=ns.by,
        )
        print(task_id)
        return 0

    if action == "list":
        tasks = ledger.state()
        for task_id in sorted(tasks):
            row = tasks[task_id]
            reviews = "".join(
                f" review={r['reviewer']}:{r['verdict']}" for r in row.get("reviews", [])
            )
            print(
                f"{task_id} [{row.get('status', '?')}]"
                f" holder={row.get('holder') or '-'}"
                f"{reviews} :: {row.get('goal', '')}"
            )
        return 0

    if action == "show":
        events = [e for e in ledger.load() if e.get("task_id") == ns.task_id]
        if not events:
            return _die(f"no events for {ns.task_id}")
        print(json.dumps(events, indent=2))
        return 0

    try:
        if action == "claim":
            ledger.claim(ns.task_id, ns.by)
            print(f"{ns.task_id} leased to {ns.by}")
            return 0

        if action == "complete":
            ledger.complete(
                ns.task_id,
                ns.by,
                status=ns.status,
                summary=ns.summary,
                files_changed=_split(ns.files),
                verification=ns.verification,
                followups=_split(ns.followups),
            )
            print(f"{ns.task_id} -> {ns.status}")
            return 0

        if action == "handoff":
            task = _require_task(ledger, ns.task_id)
            packet = handoff_packet(task, ns.to)
            if not ns.run:
                print(packet)
                return 0
            proc = spawn(ns.to, packet + "\n\nWork in this repository.", timeout=ns.timeout)
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-8:]
            print("\n".join(tail) if tail else "(no output)")
            return proc.returncode or 0

        if action == "review":
            if ns.run:
                packet = handoff_packet(_require_task(ledger, ns.task_id), ns.by)
                prompt = (
                    packet
                    + "\n\nReview this task's outcome in the current repository. "
                    + "Reply with a final line exactly 'VERDICT: approve' or "
                    + "'VERDICT: reject' followed by one sentence of evidence."
                )
                proc = spawn(ns.by, prompt, timeout=ns.timeout)
                text = (proc.stdout or proc.stderr or "").strip()
                verdict = "reject" if "VERDICT: reject" in text else (
                    "approve" if "VERDICT: approve" in text else "unparsed"
                )
                evidence = text.strip().splitlines()[-1] if text else ""
                ledger.review(ns.task_id, ns.by, verdict=verdict, evidence=evidence[:300])
                print(f"{ns.task_id} reviewed by {ns.by}: {verdict}")
                return proc.returncode or 0
            if not ns.verdict:
                return _die("--verdict is required without --run")
            ledger.review(ns.task_id, ns.by, verdict=ns.verdict, evidence=ns.evidence)
            print(f"{ns.task_id} reviewed by {ns.by}: {ns.verdict}")
            return 0

        if action == "integrate":
            if ns.accepted is None:
                return _die("pass --accept or --reject")
            ledger.integrate(ns.task_id, accepted=ns.accepted, note=ns.note)
            print(f"{ns.task_id} {'integrated' if ns.accepted else 'rejected'}")
            return 0
    except CollabError as exc:
        return _die(str(exc))

    return _die(f"unknown action {action!r}")


def _require_task(ledger, task_id) -> dict[str, Any]:
    task = ledger.state().get(task_id)
    if task is None:
        raise CollabError(f"unknown task {task_id}")
    return task
