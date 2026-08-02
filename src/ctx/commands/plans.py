"""Evidence plans and questions: plan · ask.

`plan run` carries the replan ledger that used to be a separate
`investigate` command."""

from __future__ import annotations

import sys

from ctx.commands.emit import _emit_investigation


def _read_plan_text(ws, plan_file: str) -> str | None:
    """Plan JSON from a workspace file or stdin ('-'). None ⇒ usage error
    (message already printed)."""
    if plan_file in (None, "-"):
        if sys.stdin.isatty():
            print(
                "ctx plan: no plan given (pass a JSON path or pipe stdin)",
                file=sys.stderr,
            )
            return None
        return sys.stdin.read()
    full = ws.confine(plan_file, must_exist=True)
    return full.read_text(encoding="utf-8")


def cmd_plan(ws, ns) -> int:
    """`ctx plan validate|price|run|ops` — compiled evidence plans
    (docs/EVIDENCE-PLANS.md). Validation and pricing are static: nothing
    executes; `run` executes the DAG and emits one investigation digest."""
    from ctx import plan_ir, plan_ops

    if ns.plan_cmd == "ops":
        print(plan_ops.ops_census())
        return 0

    # Read ONCE. stdin is a stream, not a file: this used to read here only
    # to check the value was not None, then read again inside the `run`
    # branch, where the second read of a pipe returns "" and every piped
    # `ctx plan run -` failed. The text is threaded through instead.
    text = _read_plan_text(ws, ns.plan_file)
    if text is None:
        return 2

    if ns.plan_cmd in ("validate", "price"):
        try:
            plan = plan_ir.parse_plan(text)
        except plan_ir.PlanError as e:
            print(f"ctx plan: {e}", file=sys.stderr)
            return 2
        rejections = plan_ir.validate_plan(plan, tier="cli", plan_policy=ws.config.plan)
        if rejections:
            print(f"[ctx plan · REJECTED · {len(rejections)} problem(s)]")
            for r in rejections:
                print("  " + r.render())
            return 2
        if ns.plan_cmd == "validate":
            print(
                f"[ctx plan · OK · {len(plan.steps)} nodes · "
                f"plan:{plan.plan_id()[:12]}]"
            )
            return 0
        print(plan_ir.price_plan(plan))
        if getattr(ns, "plan_value_explain", False):
            from ctx import plan_value as pv

            floors = pv.required_floors(plan.objective_kind, plan.requires)
            candidates = [
                pv.CandidateAction(
                    op=st.op,
                    cost_class=plan_ops.OPS[st.op].cost,
                    klass=plan_ops.OPS[st.op].klass,
                )
                for st in plan.steps
                if st.op in plan_ops.OPS
            ]
            priors = pv.load_priors(ws)
            ranked = pv.rank_followup(candidates, priors.get("operators", priors))
            declared = plan.steps[0].op if plan.steps else None
            print()
            print(pv.render_shadow(declared, ranked, floors=floors))
        return 0

    # run — one implementation, shared with what used to be `ctx plan run`
    # Pass the already-read text through: stdin is a stream and a second
    # read of a pipe returns "".
    return _run_investigation(ws, ns, text)


def _run_investigation(ws, ns, text: str | None = None) -> int:
    """`ctx plan run <plan.json>` — execute an evidence plan and return one
    digest, keeping a replan ledger: replans past the budget are declared with
    a banner and recorded, never blocked (warn and record, never stop someone
    gathering evidence).

    This was a separate `ctx plan run` command until it became clear it was
    `plan run` plus a ledger — one behaviour behind two names."""
    import hashlib as _hashlib
    import json as _json

    from ctx import plan_ir
    from ctx.plan_exec import execute_plan
    from ctx.sessiondir import session_reads_path
    from ctx.store import Store

    # `text` is threaded in from cmd_plan, which already read it. Reading
    # again here is what broke every piped `ctx plan run -`: stdin is a
    # stream, the first read drains it, and the second returned "".
    if text is None:
        text = _read_plan_text(ws, ns.plan_file)
    if text is None:
        return 2
    try:
        plan = plan_ir.parse_plan(text)
    except plan_ir.PlanError as e:
        print(f"ctx plan run: {e}", file=sys.stderr)
        return 2

    replans = ns.replans if ns.replans is not None else ws.config.plan.replans
    objective_key = _hashlib.sha256(
        " ".join(plan.question.lower().split()).encode("utf-8")
    ).hexdigest()[:12]
    ledger_dir = session_reads_path(ws.root)
    ledger = ledger_dir / "investigations.jsonl"
    prior = 0
    try:
        if ledger.is_file():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try:
                    ev = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if ev.get("objective") == objective_key:
                    prior += 1
    except OSError:
        pass

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    node_rows: dict[str, int] = {}
    out, code = execute_plan(ws, store, text, tier="cli", node_rows=node_rows)
    if code == 2:
        print(out)
        return 2

    try:
        import time as _time

        ledger_dir.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(
                _json.dumps(
                    {
                        "op": "investigate",
                        "objective": objective_key,
                        "epoch": prior + 1,
                        "plan": plan.plan_id()[:12],
                        "ts": _time.time(),  # operational only
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass

    if prior > replans:
        out += (
            f"\nreplan budget: epoch {prior + 1} for this objective exceeds the "
            f"allowance ({replans} replan(s)) — unlimited replanning degenerates "
            "to the interactive loop; patch/verify or change the hypothesis"
        )
    _emit_investigation(ws, store, out)
    if getattr(ns, "inv_advise", False):
        print()
        print(_investigate_advice(ws, plan, node_rows))
    return code


def cmd_ask(ws, ns) -> int:
    """`ctx ask "<question>" --intent <intent>` — compile a typed intent
    preset into one ctx.plan/v1 and execute it through the SAME executor
    and emission tail as investigate. No natural-language parser: the
    subject is a flag or the question's sole identifier token (disclosed);
    a missing/ambiguous slot is a teaching error that suggests, never
    acts. The disclosure rides ABOVE the digest so the interpretation is
    always visible (never hidden behind --trace)."""
    from ctx import ask
    from ctx.plan_exec import execute_plan
    from ctx.store import Store

    try:
        plan_json, disclosure = ask.compile_ask(
            ns.ask_intent,
            ns.question,
            symbol=ns.ask_symbol,
            run=ns.ask_run,
            depth=ns.ask_depth,
            ref_a=ns.ask_run,
            ref_b=getattr(ns, "ask_against", None),
            command=getattr(ns, "ask_command", None),
        )
    except ask.AskError as e:
        print(str(e), file=sys.stderr)
        return 2

    header = "\n".join(f"  {ln}" for ln in disclosure)
    if getattr(ns, "ask_show_plan", False):
        print(f"[ctx ask]\n{header}\n")
        print(plan_json)
        return 0

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    out, code = execute_plan(ws, store, plan_json, tier="cli")
    if code == 2:  # a validation rejection: the text IS the typed reason
        print(f"[ctx ask]\n{header}")
        print(out)
        return 2
    # Disclosure first, then the shared investigation emission tail.
    print(f"[ctx ask]\n{header}")
    _emit_investigation(ws, store, out)
    return code


def _investigate_advice(ws, plan, node_rows=None) -> str:
    """Shadow follow-up report for `ctx plan run --advise` (report only —
    never reorders, inserts, or suppresses anything). Shows the declared
    plan's first op against what the empirical follow-up ordering would have
    preferred among the same already-applicable candidates, with the full
    lexicographic reason; appends one ctx.shadow-rank/v1 line to the shadow
    ledger so the paired referee can score agreement offline. Floors are
    displayed descriptively from REALIZED coverage (an op's declared
    `provides` counts only when its node produced rows). Fail-open."""
    try:
        import json as _json
        import time as _time

        from ctx import plan_ops
        from ctx import plan_value as pv
        from ctx.sessiondir import session_reads_path

        floors = pv.required_floors(plan.objective_kind, plan.requires)
        coverage = pv.realized_coverage(plan.steps, node_rows or {})
        declared_first = plan.steps[0].op if plan.steps else None
        # Hard constraints FIRST: candidates are the plan's own declared ops
        # plus registered engine-available observe-class ops — the ranking
        # never introduces an action the tier could not run.
        ran_ops = [s.op for s in plan.steps]
        names = list(dict.fromkeys(ran_ops)) + sorted(
            name
            for name, spec in plan_ops.OPS.items()
            if name not in ran_ops
            and (spec.probe_available is None or spec.probe_available())
        )
        candidates = [
            pv.CandidateAction(
                op=n,
                cost_class=plan_ops.OPS[n].cost,
                klass=plan_ops.OPS[n].klass,
            )
            for n in names
            if n in plan_ops.OPS
        ]
        priors = pv.load_priors(ws)
        ranked = pv.rank_followup(candidates, priors.get("operators", priors))
        report = pv.render_shadow(
            declared_first, ranked, floors=floors, coverage=coverage
        )
        # Shadow ledger: the paired referee's input. Operational ts only.
        try:
            ldir = session_reads_path(ws.root)
            ldir.mkdir(parents=True, exist_ok=True)
            with (ldir / "shadow-rank.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    _json.dumps(
                        {
                            "schema": "ctx.shadow-rank/v1",
                            "plan": plan.plan_id()[:12],
                            "declared_first": declared_first,
                            "shadow_first": ranked[0].op if ranked else None,
                            "agreement": bool(
                                ranked and declared_first == ranked[0].op
                            ),
                            "ts": _time.time(),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        except OSError:
            pass
        return report
    except Exception as e:  # report only: never fail the investigation
        return f"(follow-up shadow report unavailable: {type(e).__name__})"
