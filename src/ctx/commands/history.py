"""Read-only replay over recorded host transcripts: `ctx replay`.

Workspace-free by design — it must run on any machine that has
~/.claude/projects, harnessed or not."""

from __future__ import annotations


def cmd_replay(ns) -> int:
    """`ctx replay` — workspace-free by design: history replay must run on
    any machine that has ~/.claude/projects, harnessed or not."""
    import json as _json

    from ctx.replay import (
        default_history_paths,
        render_regret,
        render_report,
        simulate_session,
    )

    paths = list(ns.transcripts)
    if ns.all_projects:
        paths += default_history_paths()
    if not paths:
        print("no transcripts given (pass paths or --all-projects)")
        return 1
    if ns.replay_outcomes:
        from ctx.replay import render_outcomes, session_outcomes

        events = [e for p in paths for e in session_outcomes(p)]
        if ns.replay_append_ledger:
            from ctx.sessiondir import session_reads_path
            from ctx.workspace import resolve_workspace as _rw

            _ws = _rw(ns.workspace)
            ldir = session_reads_path(_ws.root)
            ldir.mkdir(parents=True, exist_ok=True)
            # One name, used twice: the message used to report
            # evidence-outcomes.jsonl while the write went to
            # evidence-followups.jsonl, so a user who followed the message
            # found nothing there.
            ledger = ldir / "evidence-followups.jsonl"
            with ledger.open("a", encoding="utf-8") as fh:
                for e in events:
                    fh.write(_json.dumps(e.payload(), sort_keys=True) + "\n")
            print(f"appended {len(events)} events to {ledger}")
        if ns.replay_json:
            print(_json.dumps([e.payload() for e in events], indent=2))
        else:
            print(render_outcomes(events))
        return 0
    reports = [simulate_session(p) for p in paths]
    if ns.replay_json:
        print(_json.dumps(reports, indent=2))
    elif ns.replay_regret:
        print(render_regret(reports))
    else:
        print(render_report(reports, gaps=ns.gaps))
    return 0
