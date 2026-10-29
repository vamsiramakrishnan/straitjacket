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
        if action in {"prewalk", "continue"}:
            if action == "prewalk":
                ledger.claim(ns.target, "agy")
                result = ledger.start_prewalk(ns.target, model=ns.model, timeout=ns.timeout)
            else:
                result = ledger.continue_prewalk(ns.target, model=ns.model, timeout=ns.timeout)
            print(json.dumps(result, indent=2))
            return 0
        if action in {"launch", "watch", "status", "cancel", "events", "recover"}:
            from pathlib import Path
            from ctx import collab_sessions

            def root(session):
                if len(session) != 32 or any(c not in "0123456789abcdef" for c in session):
                    raise CollabError("invalid session ID")
                return Path(ws.root) / ".ctx/collab/sessions" / session

            if action == "launch":
                task = _require_task(ledger, ns.task_id)
                extra = ""
                if ns.prompt_file:
                    extra = Path(ns.prompt_file).read_text()
                    if len(extra.encode()) > 32768:
                        raise CollabError("additional prompt exceeds 32 KiB")
                ledger.claim(ns.task_id, "agy")
                packet = handoff_packet(task, "agy") + "\n\n" + extra
                result = ledger.launch_session(ns.task_id, packet, model=ns.model,
                    mode=ns.mode, conversation=ns.conversation, timeout=ns.timeout)
            elif action == "watch":
                result = collab_sessions.watch([root(s) for s in ns.sessions],
                                               until=ns.until, timeout=ns.timeout)
            elif action == "status":
                result = collab_sessions.status(root(ns.session))
            elif action == "recover":
                result = collab_sessions.recover(root(ns.session))
            elif action == "cancel":
                path = root(ns.session)
                result = collab_sessions.status(path)
                if result["status"] not in collab_sessions.TERMINAL:
                    (path / "cancel").touch()
                result = {"cancellation_requested": True, "session_id": ns.session}
            else:
                path = root(ns.session) / "events.jsonl"
                events = []
                if path.exists():
                    with path.open() as handle:
                        for line in handle:
                            try:
                                event = json.loads(line)
                            except ValueError:
                                break
                            if event["seq"] > ns.after:
                                events.append(event)
                                if len(events) == 20:
                                    break
                result = {"events": events, "next_cursor": events[-1]["seq"] if events else ns.after}
            print(json.dumps(result, indent=2))
            return 0
        if action == "plan":
            from pathlib import Path
            ledger.plan(ns.task_id, ns.by, json.loads(Path(ns.file).read_text()))
            print(f"{ns.task_id}: plan recorded")
            return 0
        if action == "progress":
            ledger.progress(ns.task_id, ns.by, ns.step, ns.status, ns.evidence)
            print(f"{ns.task_id}: step {ns.step} {ns.status}")
            return 0
        if action == "verify":
            result = ledger.verify(ns.task_id, ws.root)
            print(json.dumps(result, indent=2))
            return result["returncode"]
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
            ledger.claim(ns.task_id, ns.to)
            if ns.to == 'agy' and (ns.mode == 'accept-edits' or ns.conversation):
                raise CollabError('use durable launch for read-only resume or prewalk for isolated editing')
            proc = spawn(ns.to, packet + "\n\nWork in this repository.", timeout=ns.timeout,
                         model=ns.model, mode=ns.mode or ("plan" if ns.to == "agy" else ""), cwd=ws.root,
                         conversation=ns.conversation, structured=ns.json)
            print(proc.stdout or "(no stdout)")
            if proc.stderr:
                import sys
                print(proc.stderr, file=sys.stderr)
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
                proc = spawn(ns.by, prompt, timeout=ns.timeout, model=ns.model,
                             mode="plan" if ns.by == "agy" else "", cwd=ws.root)
                text = (proc.stdout or proc.stderr or "").strip()
                evidence = text.strip().splitlines()[-1] if text else ""
                verdict = "approve" if not proc.returncode and evidence.startswith("VERDICT: approve ") else "reject"
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
    except (CollabError, OSError, ValueError) as exc:
        return _die(str(exc))

    return _die(f"unknown action {action!r}")


def _require_task(ledger, task_id) -> dict[str, Any]:
    task = ledger.state().get(task_id)
    if task is None:
        raise CollabError(f"unknown task {task_id}")
    return task
