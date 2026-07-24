"""The input side of containment: `ctx surface …`."""

from __future__ import annotations


def cmd_surface(ws, ns) -> int:
    """`ctx surface {inventory,audit,explain,trim}` — the input side of
    containment: measure the discretionary capability surface, never mutate it
    (Phase 1). All rendering is bounded and deterministic."""
    import json as _json

    from ctx import surface

    if ns.surface_cmd == "install-gateway":
        from ctx.installer import install_gateway

        print(install_gateway(ws, ns.host, apply=ns.apply))
        return 0

    if ns.surface_cmd in ("reconcile", "referee"):
        from ctx import surface_reconcile as sr

        if ns.surface_cmd == "referee":
            rep = sr.referee(ws.root)
            print(_json.dumps(rep, indent=2) if ns.json else
                  f"[ctx surface referee] hides scored: {rep['hides_scored']} · "
                  f"safe {rep['safe']} · unsafe {rep['unsafe']} · verdict {rep['verdict']}"
                  + ("\n  promotable: " + ", ".join(rep["promotable"]) if rep["promotable"] else ""))
            return 0
        rep = sr.run_reconcile(ws.root, intent=ns.intent, phase=ns.phase, enforce=ns.enforce)
        print(_json.dumps(rep, indent=2) if ns.json else sr.render_reconcile(rep))
        return 0

    if ns.surface_cmd == "compile":
        from ctx import surface_profiles

        if not ns.profile:
            print("usage: ctx surface compile --profile <name> [--host HOST] [--apply]")
            print("built-in profiles: " + ", ".join(surface_profiles.BUILTIN_PROFILES))
            return 2
        rep = surface_profiles.compile_profile(
            ws.root, ns.profile, host=ns.host, apply=ns.apply,
            probe_mcp=getattr(ns, "probe_mcp", False))
        if ns.json:
            print(_json.dumps(rep, indent=2))
            return 0
        print(surface_profiles.render_compile(rep))
        return 1 if rep.get("error") else 0

    if ns.surface_cmd == "explain":
        if not ns.target:
            print("usage: ctx surface explain <capability-id>")
            return 2
        records = surface.detect_overlaps(surface.collect_surface(ws.root))
        counts = surface.observed_tool_counts(ws.root)
        records = [surface._with(c, invocations=surface._match_invocations(c, counts))
                   for c in records]
        match = next((c for c in records if c.id == ns.target), None)
        if match is None:
            print(f"no capability {ns.target!r}; run `ctx surface inventory`")
            return 1
        print(surface.render_explain(match))
        return 0

    if ns.surface_cmd == "inventory":
        base = surface.collect_surface(ws.root)
        if getattr(ns, "probe_mcp", False):
            probed = surface.probe_surface(ws.root)
            provs = {p.provider for p in probed}
            base = [c for c in base
                    if not (c.kind == "mcp_server" and c.provider in provs)] + probed
        records = surface.detect_overlaps(base)
        counts = surface.observed_tool_counts(ws.root)
        records = [surface._with(c, invocations=surface._match_invocations(c, counts))
                   for c in records]
        if ns.json:
            print(_json.dumps([c.as_dict() for c in records], indent=2))
        else:
            print(surface.render_inventory(records))
        return 0

    # audit / trim / graph build the full audit.
    a = surface.audit(ws.root, probe_mcp=getattr(ns, "probe_mcp", False))
    if ns.json:
        print(_json.dumps(a, indent=2))
        return 0
    if ns.surface_cmd == "graph":
        recs = [surface.Capability(**{k: (tuple(v) if isinstance(v, list) else v)
                                      for k, v in r.items() if k != "recommended_level"})
                for r in a["records"]]
        print(surface.render_graph(recs, a["graph"]))
        return 0
    if ns.surface_cmd == "trim":
        tp = a["trim_preview"]
        print("[ctx surface trim --preview · advisory only, nothing hidden]")
        if not tp["ids"]:
            print("  nothing to defer: surface is already lean")
            return 0
        for cid in tp["ids"]:
            rec = next(c for c in a["records"] if c["id"] == cid)
            print(f"  defer  {rec['tokens']:>6,} tok  {cid:<28} "
                  f"auth={rec['authority']} → {rec['recommended_level']}")
        print(f"  ── est {tp['est_token_reduction']:,} tokens/turn recoverable")
        return 0
    print(surface.render_audit(a))
    return 0
