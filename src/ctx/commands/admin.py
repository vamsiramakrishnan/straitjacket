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
    print("  ctx wrap setup        hook ctx into the agents you have installed")
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
    days = ns.retention_days or ws.config.store.retention_days
    result = store.gc(days)
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
    from ctx.store import Store

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    if ns.show:
        print(show_checkpoint(store, ws, ns.show))
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
        print("no telemetry yet — run some commands under the harness first")
        return 1
    total_raw = sum(s["raw"] for s in per_op.values())
    total_emitted = sum(s["emitted"] for s in per_op.values())
    saved_tok = max(0, (total_raw - total_emitted) // 4)
    print(f"[ctx gain · workspace {ws.workspace_id[:12]}]")
    print(
        f"contained: {total_raw:,} bytes raw -> {total_emitted:,} bytes emitted "
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
            f"  {op:7s} {s['events']:>5,} events · {s['raw']:>12,} B -> "
            f"{s['emitted']:>10,} B ({ratio:.1f}x)"
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
