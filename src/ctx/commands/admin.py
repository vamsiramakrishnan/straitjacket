"""Keep-track and upkeep verbs: init · doctor · gc · pin ·
checkpoint · debt · gain · policy."""

from __future__ import annotations

import sys


def cmd_init(ws, ns) -> int:
    from ctx.installer import init_workspace

    print("\n".join(init_workspace(ws.root)) or "nothing to do")
    # Writing two config files is not a result a user can feel. Say what
    # it bought them and where to go next, so init is never a dead end.
    print()
    print("This repo now has ctx settings (edit ctx.toml to tune budgets).")
    print("Next:")
    print("  ctx setup        hook ctx into the agents you have installed")
    print("  ctx run -- pytest -q  try it on something noisy")
    return 0


def cmd_doctor(ws, ns) -> int:
    from ctx.installer import doctor_report

    report = doctor_report(ws, antigravity=ns.antigravity)
    print(report)
    return 0 if "PROBLEMS" not in report.splitlines()[0] else 1


def cmd_gc(ws, ns) -> int:
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    from ctx import bounds

    # bounds.explicit, not `or`: --retention-days 0 means collect everything
    # already expired. `or` made the one spelling that means 'now' the one
    # spelling that silently did nothing.
    days = int(bounds.explicit(ns.retention_days, ws.config.store.retention_days))
    # An explicitly supplied horizon is the user overriding the configured
    # policy, so it outranks the retention leases that policy minted.
    result = store.gc(days, override_retention=ns.retention_days is not None)
    print(
        f"gc: removed {result['blobs_removed']} blobs, "
        f"{result['manifests_removed']} manifests (retention {days}d)"
    )
    return 0


def cmd_pin(ws, ns) -> int:
    from ctx.commands.retrieve import _bad_input_errors, _fail
    from ctx.refs import parse_ref
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    try:
        ref = parse_ref(ns.ref)
        store.pin(ref.id or "")
    except _bad_input_errors() as e:
        # `pin` takes the same handles as `get`; it owes the same attributed
        # message and the same exit code (docs/CLI.md, "Exit codes").
        return _fail("pin", e)
    print(f"pinned {ref.display()}")
    return 0


def cmd_checkpoint(ws, ns) -> int:
    from ctx.checkpoint import create_checkpoint, show_checkpoint
    from ctx.commands._errors import bad_input_errors, fail
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    if ns.show:
        # `--show` takes the same handles as `get`, so it owes the same
        # attributed message and the same exit code. Without this an
        # unresolvable checkpoint fell through to cli.py's blanket handler
        # and exited 1 -- indistinguishable, to a calling script, from ctx
        # itself failing -- while the identical mistake through `ctx get`
        # exited 2 as documented.
        try:
            print(show_checkpoint(store, ws, ns.show))
        except bad_input_errors() as e:
            return fail("checkpoint", e)
        return 0
    if not ns.goal:
        print("ctx checkpoint: --goal is required (or --show <checkpoint:id>)", file=sys.stderr)
        return 2
    _, doc = create_checkpoint(
        store,
        ws,
        goal=ns.goal,
        state=ns.state,
        decisions=ns.decisions,
        hypotheses=ns.hypotheses,
        evidence=ns.evidence,
        attempted=ns.attempted,
        files=ns.files,
    )
    print(doc)
    return 0


def cmd_debt(ws, ns) -> int:
    """`ctx debt add|list|resolve` — track work deliberately deferred."""
    from ctx import debt as _debt

    if ns.debt_cmd == "add":
        eid = _debt.add(ws.root, ns.note, ref=ns.ref)
        print(f"declared: {eid}")
        return 0
    if ns.debt_cmd == "resolve":
        ok = _debt.resolve(ws.root, ns.id)
        print("resolved" if ok else f"unknown debt id: {ns.id}")
        return 0 if ok else 1
    print(_debt.render(ws.root))
    return 0


def cmd_gain(ws, ns=None) -> int:
    """Cumulative containment savings, made legible (rtk's `gain` lesson:
    the metric users can watch is the metric that keeps the harness on).
    `ctx gain` takes no options; ns is accepted for dispatch uniformity."""
    import json as _json

    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    path = store.audit_dir / "telemetry.jsonl"
    per_op: dict[str, dict[str, int]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ev = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            op = str(ev.get("op") or "?")
            slot = per_op.setdefault(op, {"events": 0, "raw": 0, "emitted": 0})
            slot["events"] += 1
            slot["raw"] += int(ev.get("raw_bytes", 0))
            slot["emitted"] += int(ev.get("emitted_bytes", 0))
    if not per_op:
        # Two failures in one line, before: it went to stdout while every
        # other error goes to stderr, and it blamed the user for not having
        # run anything — when the usual cause is that nothing is hooked, so
        # commands ran and were never intercepted. `ctx doctor` answers
        # exactly that question ("an agent is wrapped"), so send them there.
        print(
            "ctx gain: no telemetry for this workspace yet.\n"
            "  Most often this means no agent is hooked, not that nothing ran —\n"
            "  commands went straight to the shell and ctx never saw them.\n"
            "  Check:  ctx doctor        (look for 'an agent is wrapped')\n"
            "  Fix:    ctx setup    (hook the agents you have installed)",
            file=sys.stderr,
        )
        return 1
    total_raw = sum(s["raw"] for s in per_op.values())
    total_emitted = sum(s["emitted"] for s in per_op.values())
    saved_tok = max(0, (total_raw - total_emitted) // 4)
    # Byte sizes go through the one shared formatter (`1.4 MiB`), the same
    # one every digest header uses — this was the last place rendering raw
    # `{n:,} bytes` by hand.
    from ctx.textutil import fmt_bytes

    print(f"[ctx gain · workspace {ws.workspace_id[:12]}]")
    print(
        f"contained: {fmt_bytes(total_raw)} raw -> {fmt_bytes(total_emitted)} emitted "
        f"({total_raw / max(1, total_emitted):.1f}x)"
    )
    print(f"est tokens kept out of context: {saved_tok:,}")
    # Input-price framing only; the real savings compound through cache reads.
    # Price against the session's own model when the proxy recorded one
    # (host-neutral: works for Gemini/GPT/Claude alike); otherwise show a
    # cheap->premium band from the shipped table rather than naming one vendor.
    from ctx import pricing
    from ctx.engagement import session_model

    model = session_model(ws.root)
    if model:
        p = pricing.price_for(model, workspace_root=ws.root)
        print(
            f"est spend avoided (input-priced): ~${saved_tok * p.input / 1e6:.2f} "
            f"({model} @ ${p.input:g}/Mtok in)"
        )
    else:
        # No session model recorded yet. A min-to-max sweep of the whole price
        # table spans ~100x, which is not an answer a human can use. Quote one
        # number at the table's mid-tier fallback, name the assumption, and say
        # how to make it exact.
        rate = float((pricing.load_table(ws.root).get("fallback") or {}).get("in", 3.0))
        print(
            f"est spend avoided (input-priced): ~${saved_tok * rate / 1e6:.2f} "
            f"(assuming ${rate:g}/Mtok in — no model seen yet; "
            f"run under `ctx wrap` for your real rate)"
        )
    print("by verb:")
    for op, s in sorted(per_op.items(), key=lambda kv: -kv[1]["raw"]):
        ratio = s["raw"] / max(1, s["emitted"])
        print(
            f"  {op:7s} {s['events']:>5,} events · {fmt_bytes(s['raw']):>12} -> "
            f"{fmt_bytes(s['emitted']):>10} ({ratio:.1f}x)"
        )
    return 0


def cmd_policy(ws, ns) -> int:
    if ns.policy_cmd == "show":
        path = ws.root / "ctx-policy.toml"
        if path.is_file():
            print(path.read_text(encoding="utf-8"))
        else:
            print("no compiled policy")
        return 0
    try:
        from ctx.policy import compile_policy, render_policy, write_policy
    except ImportError:
        print("policy module not available", file=sys.stderr)
        return 1
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    kwargs = {"min_runs": ns.min_runs} if ns.min_runs is not None else {}
    if getattr(ns, "plan_value", False):
        from ctx.policy import compile_plan_value

        kwargs["plan_value"] = compile_plan_value(ws)
    policy = compile_policy(store, ws, **kwargs)
    print(render_policy(policy))
    print(f"written: {write_policy(ws, policy)}")
    return 0


def cmd_ladders(ws, ns) -> int:
    """`ctx ladders` — the conditionality audit, measured rather than asserted.

    The "measured today?" column in docs/LADDERS.md was hand-maintained, which
    made it the part of the audit most likely to drift into advertising. This
    derives it: a ladder is measurable when it declares a signal naming a
    ledger that exists, and the report shows the real distribution or says
    exactly why there is none.
    """
    import json as _json

    from ctx import ladders as _ladders

    raw = _raw_ladders_config(ws)
    corpus = getattr(ns, "ladders_corpus", None)
    if corpus:
        roots = _ladders.discover_workspaces(corpus)
        if getattr(ns, "ladders_json", False):
            print(_json.dumps({
                "schema": "ctx.ladders/v1",
                "corpus": str(corpus),
                "workspaces": len(roots),
                "ladders": [
                    {"key": lad.key, "name": lad.name,
                     **_ladders.measure_corpus(roots, lad)}
                    for lad in _ladders.configured(raw)
                ],
            }, indent=2, sort_keys=True))
            return 0
        print(_ladders.report_corpus(corpus, raw))
        return 0
    if getattr(ns, "ladders_json", False):
        out = {
            "schema": "ctx.ladders/v1",
            "ladders": [
                {
                    "key": lad.key,
                    "name": lad.name,
                    "axis": lad.axis,
                    "rungs": list(lad.rungs),
                    "traversed_by": lad.traversed_by,
                    "latching": lad.latching,
                    **_ladders.measure(ws.root, lad),
                }
                for lad in _ladders.configured(raw)
            ],
            "config_problems": _ladders.validate(raw),
        }
        print(_json.dumps(out, indent=2, sort_keys=True))
        return 0
    print(_ladders.report(ws.root, raw))
    return 0


def _raw_ladders_config(ws) -> dict:
    """The `[ladders]` table straight from ctx.toml.

    Read raw rather than through Config: rung lists are a per-ladder open
    vocabulary, and threading them through the typed Config dataclass would
    mean a field per ladder — the copy-per-consumer this registry exists to
    remove.
    """
    import tomllib

    path = ws.root / "ctx.toml"
    try:
        with open(path, "rb") as fh:
            return (tomllib.load(fh) or {}).get("ladders") or {}
    except (OSError, ValueError):
        return {}
