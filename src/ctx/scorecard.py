"""Session scorecard: runtime cache/cost/effort economics from wire ground truth.

Mechanisms D+F. Every wrapped session that ran behind the Tier-0 observer
gets a scorecard computed from ``wire.jsonl`` — the same decomposition the
benchmark postmortems had to reconstruct by hand:

- token classes (uncached input / cache read / cache creation / output)
- cold_prefix_tok: the one-time full-prefix write (first content request
  that created >4k tokens reading nothing) — an amortized cost, not churn
- invalidations: true cache breaks (cache_read regressing vs the running
  max on a multi-message thread) — the only churn signal that matters
- timing split: connect / ttfb (queue+prefill) / generation
- effort mix: tool_use census by tool name; edit share separates
  "did less work" from "said less about it" (the S2 test-thinning guard)

Cost totals come from the host when available; the decomposition here uses
published per-MTok prices and is labeled an estimate.

Axis discovery (REFLEX): the scorecard additionally folds the session's
behavioral ledgers (``.ctx-session-reads/reflex-outcomes.jsonl``,
``.ctx-session-reads/eval-adoption.jsonl``) into a behavioral-anomalies
section — starvation re-runs per command signature, landings, densify
actions, eval opportunities vs taught. This is the single-session view of
the spec3 failure (8× pytest re-runs, 0 hint follow-through) that no
benchmark should be needed to see. Fail-open like everything else here:
missing or corrupt ledgers mean the section is absent, never an error, and
zero events render no block — the scorecard must not grow noise.
"""

from __future__ import annotations

import json
from pathlib import Path

from ctx import pricing
from ctx.gitstatus import changed_paths
from ctx.sessiondir import session_reads_path

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "write_to_file", "replace_file_content"}

# ----------------------------------------------------------- scorecard v2
# Intervention-ledger vocabulary (docs/EDC.md §9/§20). The v2 ledger
# (``.ctx-session-reads/interventions.jsonl``) carries emission lines
# (ctx.intervention/v1) and outcome lines (ctx.intervention-outcome/v1);
# unknown events and outcomes are future schema, never errors.
_V2_OUTCOME_KEYS = {
    "retrieval_landing": "landings",
    "progressed_without_retrieval": "progressed",
    "equivalent_rerun": "equivalent_reruns",
    "slicer_rerun": "slicer_reruns",
    "narrowed_execution": "narrowed",
    "validation_after_edit": "validated_after_edit",
    "verbatim_retry": "verbatim_retries",
    "workaround": "workarounds",
    "expired_unresolved": "expired",
}
# A confirmed starvation resolved WITHOUT a re-execution after adaptation.
_RESOLVED_WITHOUT_RERUN = {
    "retrieval_landing", "progressed_without_retrieval", "narrowed_execution",
}
# Plan modes that are adaptations (the controller responded to starvation).
_ADAPTED_PLAN_MODES = {"dense", "bypass"}


def _behavioral_anomalies(session_reads_dir: Path) -> dict | None:
    """Fold the behavioral ledgers into an anomalies dict, or None when
    there is nothing to say (no parseable events at all).

    Reads two append-only fail-open ledgers from ``.ctx-session-reads/``:

    - ``reflex-outcomes.jsonl`` — one line per behavioral event scored by
      the reflex arc: ``{"ts", "event": "starvation"|"landing"|"friction",
      "signature", "run", "action": "densify"|"none"}``
    - ``eval-adoption.jsonl`` — ``{"op": "eval_opportunity", "taught",
      "ts"}`` (the teaching denominator)

    Corrupt lines are skipped individually; unreadable files contribute
    nothing. Never raises."""
    starvation = landings = friction = densified = 0
    per_sig: dict[str, int] = {}
    try:
        lines = (
            (session_reads_dir / "reflex-outcomes.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        event = rec.get("event")
        if event == "starvation":
            starvation += 1
            sig = str(rec.get("signature") or "?")
            per_sig[sig] = per_sig.get(sig, 0) + 1
        elif event == "landing":
            landings += 1
        elif event == "friction":
            friction += 1
        else:
            continue  # unknown event kinds are future schema, not errors
        if rec.get("action") == "densify":
            densified += 1
    opportunities = taught = 0
    try:
        elines = (
            (session_reads_dir / "eval-adoption.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        elines = []
    for line in elines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("op") == "eval_opportunity":
            opportunities += 1
            if rec.get("taught"):
                taught += 1
    if starvation + landings + friction == 0 and opportunities == 0:
        return None
    return {
        "starvation": starvation,
        "starvation_signatures": dict(sorted(per_sig.items())),
        "landings": landings,
        "friction": friction,
        "ratio": f"{starvation}:{landings}",
        "densified": densified,
        "eval_opportunities": opportunities,
        "eval_taught": taught,
    }


def _median(values: list[float]) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _xy(value) -> tuple[int, int] | None:
    """Tolerant x/y reader for coverage fields: accepts ``[x, y]``,
    ``"x/y"``, or a dict with common covered/total key spellings.
    None when underivable — the metric is then omitted, never invented."""
    try:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return int(value[0]), int(value[1])
        if isinstance(value, str) and "/" in value:
            a, b = value.split("/", 1)
            return int(a), int(b)
        if isinstance(value, dict):
            for xk in ("covered", "have", "count", "x"):
                for yk in ("total", "required", "of", "y"):
                    if xk in value and yk in value:
                        return int(value[xk]), int(value[yk])
    except (TypeError, ValueError):
        return None
    return None


def _runtime_s(rec: dict) -> float | None:
    """Observed runtime carried by an event, in seconds. Only ever read
    from the event itself (or its evidence map) — never invented."""
    sources = [rec]
    ev = rec.get("evidence")
    if isinstance(ev, dict):
        sources.append(ev)
    for src in sources:
        for key in ("runtime_ms", "runtimeMs", "duration_ms", "durationMs"):
            v = src.get(key)
            if isinstance(v, (int, float)) and v >= 0:
                return v / 1000.0
        for key in ("runtime_s", "runtimeS"):
            v = src.get(key)
            if isinstance(v, (int, float)) and v >= 0:
                return float(v)
    return None


def _resolution(outcomes: list[str]) -> str | None:
    """The episode's resolution: the LAST non-censored outcome.
    ``expired_unresolved`` is a censored observation (EDC §9) — it never
    resolves anything and is excluded from every rate denominator."""
    resolved = [o for o in outcomes if o != "expired_unresolved"]
    return resolved[-1] if resolved else None


def _interventions_v2(session_reads_dir: Path) -> dict | None:
    """Fold ``interventions.jsonl`` (v2 ledger, EDC §9/§20) into the
    scorecard's behavioral-outcomes section, or None when the ledger is
    absent or yields no interventions — the v1 rendering then stays
    byte-identical. Corrupt lines are skipped individually; never raises."""
    try:
        lines = (
            (session_reads_dir / "interventions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        return None
    ivs: dict[str, dict] = {}
    order: list[str] = []
    pending_outcomes: list[tuple[str, str, dict]] = []
    transitions: dict[str, dict[str, int]] = {}
    runtimes: dict[str, list[float]] = {}
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        event = str(rec.get("event") or "")
        if event == "intervention_emitted":
            iid = str(rec.get("interventionId") or "")
            if not iid:
                continue
            cov = rec.get("coverage")
            info = {
                "family": str(rec.get("family") or "?"),
                "signature": str(rec.get("signature") or "?"),
                "seq": rec.get("sessionSeq"),
                "generation": rec.get("generation"),
                "plan_mode": str(rec.get("planMode") or "").lower(),
                "coverage": cov if isinstance(cov, dict) else {},
                "hints": int(rec.get("hints") or 0),
                "outcomes": [],
            }
            if iid not in ivs:
                order.append(iid)
            ivs[iid] = info
            rs = _runtime_s(rec)
            if rs is not None:
                runtimes.setdefault(info["signature"], []).append(rs)
        elif event == "intervention_outcome":
            iid = str(rec.get("interventionId") or "")
            outcome = str(rec.get("outcome") or "")
            if iid and outcome:
                pending_outcomes.append((iid, outcome, rec))
        elif event.endswith("transition"):
            fam = str(rec.get("family") or "?")
            to = str(rec.get("to") or rec.get("planMode") or "?").lower()
            fam_t = transitions.setdefault(fam, {})
            fam_t[to] = fam_t.get(to, 0) + 1
        # any other event kind: future schema, skipped
    if not ivs:
        return None
    for iid, outcome, rec in pending_outcomes:
        info = ivs.get(iid)
        if info is None:
            continue  # unattributable outcome — skipped, fail-open
        info["outcomes"].append(outcome)
        rs = _runtime_s(rec)
        if rs is not None:
            runtimes.setdefault(info["signature"], []).append(rs)

    families: dict[str, dict] = {}
    for iid in order:
        info = ivs[iid]
        f = families.setdefault(info["family"], {
            "events": 0, "census_complete": 0, "hinted": 0,
            "hinted_resolved": 0, "hinted_landed": 0,
            "landings": 0, "progressed": 0, "equivalent_reruns": 0,
            "slicer_reruns": 0, "narrowed": 0, "validated_after_edit": 0,
            "verbatim_retries": 0, "workarounds": 0, "expired": 0,
            "other_outcomes": 0,
            "_required_fractions": [], "_named": [0, 0], "_named_n": 0,
            "_addressable": [0, 0], "_addressable_n": 0,
        })
        f["events"] += 1
        cov = info["coverage"]
        rf = cov.get("requiredFraction")
        if isinstance(rf, (int, float)):
            f["_required_fractions"].append(float(rf))
            if float(rf) == 1.0:
                f["census_complete"] += 1
        named = _xy(cov.get("named"))
        if named is not None:
            f["_named"][0] += named[0]
            f["_named"][1] += named[1]
            f["_named_n"] += 1
        addressable = _xy(cov.get("addressable"))
        if addressable is not None:
            f["_addressable"][0] += addressable[0]
            f["_addressable"][1] += addressable[1]
            f["_addressable_n"] += 1
        for outcome in info["outcomes"]:
            key = _V2_OUTCOME_KEYS.get(outcome)
            if key:
                f[key] += 1
            else:
                f["other_outcomes"] += 1  # tolerant reader: unknown → other
        resolution = _resolution(info["outcomes"])
        if info["hints"] > 0:
            f["hinted"] += 1
            if resolution is not None:
                f["hinted_resolved"] += 1
                if resolution == "retrieval_landing":
                    f["hinted_landed"] += 1
    for fam, f in families.items():
        fracs = f.pop("_required_fractions")
        f["required_pct"] = (
            round(100 * sum(fracs) / len(fracs), 1) if fracs else None
        )
        named, named_n = f.pop("_named"), f.pop("_named_n")
        f["named"] = f"{named[0]}/{named[1]}" if named_n else None
        addr, addr_n = f.pop("_addressable"), f.pop("_addressable_n")
        f["addressable"] = f"{addr[0]}/{addr[1]}" if addr_n else None
        if fam in transitions:
            f["transitions"] = dict(sorted(transitions[fam].items()))

    # Estimated downstream cost (EDC §20 amendment): every counterfactual
    # is a labeled estimate carrying a conservative derivation; a metric
    # whose inputs are absent is omitted, never invented.
    adapted = [ivs[iid] for iid in order
               if ivs[iid]["plan_mode"] in _ADAPTED_PLAN_MODES]
    estimates = None
    if adapted:
        avoided = [i for i in adapted
                   if _resolution(i["outcomes"]) in _RESOLVED_WITHOUT_RERUN]
        estimates = {
            "avoided_reexecutions": len(avoided),
            "avoided_turns": len(avoided),
        }
        meds: list[float] | None = []
        for info in avoided:
            obs = runtimes.get(info["signature"])
            med = _median(obs) if obs else None
            if med is None:
                meds = None  # any unobserved signature → omit, don't invent
                break
            meds.append(med)
        if avoided and meds is not None:
            estimates["avoided_runtime_s"] = round(sum(meds), 1)

    by_sig: dict[str, list[dict]] = {}
    for iid in order:
        info = ivs[iid]
        by_sig.setdefault(info["signature"], []).append(info)
    episodes = []
    for sig, infos in sorted(by_sig.items()):
        infos = sorted(
            infos,
            key=lambda i: i["seq"] if isinstance(i["seq"], int) else 1 << 30,
        )
        first = infos[0]
        responses: list[str] = []
        outcome_seq: list[str] = []
        for info in infos:
            mode = info["plan_mode"] or "normal"
            if not responses or responses[-1] != mode:
                responses.append(mode)
            if info["outcomes"]:
                outcome_seq.extend(info["outcomes"])
            else:
                outcome_seq.append("unresolved")
        episodes.append({
            "signature": sig,
            "family": first["family"],
            "first_seq": first["seq"],
            "generation": first["generation"],
            "responses": responses,
            "outcomes": outcome_seq,
        })

    result: dict = {
        "families": {k: dict(v) for k, v in sorted(families.items())},
        "episodes": episodes,
    }
    if estimates is not None:
        result["estimates"] = estimates
    return result


def compute_scorecard(proxy_state_dir: Path) -> dict | None:
    """Fold wire.jsonl into a scorecard dict. None when no observations.

    When the session dir's parent (``.ctx-session-reads/``) carries
    behavioral ledgers with events, the dict gains an ``anomalies`` section
    (see ``_behavioral_anomalies``)."""
    wire = Path(proxy_state_dir) / "wire.jsonl"
    if not wire.is_file():
        return None
    tok = {"input": 0, "read": 0, "creation": 0, "output": 0}
    ms = {"connect": 0.0, "ttfb": 0.0, "gen": 0.0}
    tools: dict[str, int] = {}
    per_model: dict[str, dict[str, int]] = {}
    requests = invalidations = 0
    rounds = 0  # main-thread model rounds (multi-message requests) — the
    # true unit of session cost (Tura wave): each is ttfb + a suffix write
    cold_prefix = 0
    # Per-thread read tracking (metrology fix): parallel tool-call models
    # interleave requests from several transcript threads; comparing a side
    # thread's cache_read against a single global max misreported prefix
    # regressions. Each request joins the thread with the largest message
    # count <= its own (transcripts only grow); regressions are judged
    # within that thread only.
    threads: list[dict] = []  # {"msgs": int, "max_read": int}
    est_cost = 0.0
    first_rescued_round: int | None = None
    rescued_blocks = 0
    try:
        lines = wire.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(rec.get("path", "")).endswith("/messages"):
            continue
        usage = rec.get("usage") or {}
        if not usage:
            continue
        requests += 1
        if rec.get("messages", 0) > 1:
            rounds += 1
            if rec.get("rescued"):
                rescued_blocks = max(rescued_blocks, int(rec["rescued"]))
                if first_rescued_round is None:
                    first_rescued_round = rounds
        u_in = usage.get("input_tokens", 0)
        u_read = usage.get("cache_read_input_tokens", 0)
        u_cre = usage.get("cache_creation_input_tokens", 0)
        u_out = usage.get("output_tokens", 0)
        tok["input"] += u_in
        tok["read"] += u_read
        tok["creation"] += u_cre
        tok["output"] += u_out
        model = str(rec.get("model") or "")
        pm = per_model.setdefault(model or "unknown", {"in": 0, "read": 0, "cre": 0, "out": 0})
        pm["in"] += u_in
        pm["read"] += u_read
        pm["cre"] += u_cre
        pm["out"] += u_out
        est_cost += pricing.cost_usd(
            {"input": u_in, "cache_read": u_read, "cache_write": u_cre, "output": u_out},
            model,
        )
        msgs = int(rec.get("messages", 0) or 0)
        if u_read == 0 and u_cre > 4096 and cold_prefix == 0:
            cold_prefix = u_cre
        elif msgs > 1:
            thread = None
            for t in threads:
                if t["msgs"] <= msgs and (thread is None or t["msgs"] > thread["msgs"]):
                    thread = t
            if thread is None:
                thread = {"msgs": 0, "max_read": 0}
                threads.append(thread)
            # `u_read and ...` skipped exactly the worst case: cache_read
            # collapsing to 0 on an established thread is a full prefix
            # rewrite, and 0 is a value here (usage-less records were
            # dropped above), not an absence.
            if u_read < thread["max_read"]:
                invalidations += 1
            thread["msgs"] = msgs
            thread["max_read"] = max(thread["max_read"], u_read)
        m = rec.get("ms") or {}
        ms["connect"] += m.get("connect", 0.0)
        ms["ttfb"] += m.get("ttfb", 0.0)
        ms["gen"] += max(0.0, m.get("total", 0.0) - m.get("ttfb", 0.0))
        for name, n in (rec.get("tools") or {}).items():
            tools[name] = tools.get(name, 0) + int(n)
    if requests == 0:
        return None
    prompt = tok["input"] + tok["read"] + tok["creation"]
    total_tools = sum(tools.values())
    edits = sum(n for name, n in tools.items() if name in _EDIT_TOOLS)
    sc: dict = {
        "schema": "ctx.scorecard/v1",
        "requests": requests,
        "rounds": rounds,
        "tokens": dict(tok),
        "cache_hit_pct": round(100 * tok["read"] / prompt, 1) if prompt else 0.0,
        "cold_prefix_tok": cold_prefix,
        "invalidations": invalidations,
        "output_per_request": round(tok["output"] / requests, 1),
        "est_cost_usd": round(est_cost, 4),
        "timing_s": {k: round(v / 1000, 1) for k, v in ms.items()},
        "tools": dict(sorted(tools.items())),
        "edit_share_pct": round(100 * edits / total_tools, 1) if total_tools else 0.0,
        "per_model": {k: dict(v) for k, v in sorted(per_model.items())},
    }
    try:
        anomalies = _behavioral_anomalies(Path(proxy_state_dir).parent)
        if anomalies:
            sc["anomalies"] = anomalies
    except Exception:
        pass  # fail-open: the anomalies section is absent, never an error
    try:
        interventions = _interventions_v2(Path(proxy_state_dir).parent)
        if interventions:
            sc["interventions"] = interventions
    except Exception:
        pass  # fail-open: v2 section absent, v1 rendering byte-identical
    if first_rescued_round is not None:
        # Post-rescue recovery cost (Tura's best metric, adopted): how much
        # of the session ran after rescue began — the price of regaining
        # momentum on an elided transcript.
        sc["rescue_recovery"] = {
            "first_rescued_round": first_rescued_round,
            "rounds_after": rounds - first_rescued_round,
            "blocks_elided": rescued_blocks,
        }
    return sc


def render_scorecard(sc: dict) -> str:
    """Bounded human-readable rendering for `ctx stats --session`."""
    t = sc["tokens"]
    lines = [
        "session scorecard (wire ground truth):",
        f"  requests: {sc['requests']} · est cost ${sc['est_cost_usd']:.3f} (decomposition estimate)",
        f"  tokens: out {t['output']:,} · cache read {t['read']:,} · "
        f"cache write {t['creation']:,} · uncached in {t['input']:,}",
        f"  cache: hit {sc['cache_hit_pct']}% · cold-prefix {sc['cold_prefix_tok']:,} tok "
        f"(one-time) · true invalidations {sc['invalidations']}",
        f"  emission: {sc['output_per_request']} tok/request",
        f"  time: prefill+queue {sc['timing_s']['ttfb']}s · generation {sc['timing_s']['gen']}s "
        f"· connect {sc['timing_s']['connect']}s",
    ]
    if sc["tools"]:
        census = " ".join(f"{k}×{v}" for k, v in list(sc["tools"].items())[:8])
        lines.append(f"  effort: edit-share {sc['edit_share_pct']}% · {census}")
    an = sc.get("anomalies")
    if an:
        # Behavioral anomalies (REFLEX axis discovery): rendered only when
        # the ledgers had something to say — a zero-event session shows no
        # block, keeping the scorecard byte-identical to the pre-reflex one.
        sigs = an.get("starvation_signatures") or {}
        head = f"{an['starvation']} starvation"
        if sigs:
            shown = ", ".join(f"'{s}'" for s in list(sigs)[:4])
            plural = "s" if len(sigs) != 1 else ""
            head += f" ({len(sigs)} signature{plural}: {shown})"
        parts = [head, f"{an['landings']} landings"]
        if an.get("friction"):
            parts.append(f"{an['friction']} friction")
        parts.append(f"densified: {'yes' if an.get('densified') else 'no'}")
        lines.append("  anomalies: " + " · ".join(parts))
        if an.get("eval_opportunities"):
            lines.append(
                f"  eval adoption: {an['eval_opportunities']} opportunities "
                f"· {an['eval_taught']} taught"
            )
    iv = sc.get("interventions")
    if iv:
        lines.extend(_render_interventions(iv))
    return "\n".join(lines)


def _render_interventions(iv: dict) -> list[str]:
    """Scorecard v2 behavioral blocks (EDC §20): per-family outcomes,
    labeled downstream-cost estimates, evidence-coverage table, and
    per-signature episode narratives. Rendered only when the v2 ledger
    yielded interventions — absent ledger keeps v1 output byte-identical."""
    lines: list[str] = ["  interventions (v2 ledger):"]
    for fam, f in iv["families"].items():
        lines.append(
            f"    {fam}: {f['events']} interventions · census complete "
            f"{f['census_complete']}/{f['events']} · hinted {f['hinted']}"
        )
        parts = [
            f"landings {f['landings']}",
            f"progressed w/o retrieval {f['progressed']}",
            f"equivalent reruns {f['equivalent_reruns']}",
            f"slicer reruns {f['slicer_reruns']}",
        ]
        for label, key in (
            ("narrowed", "narrowed"),
            ("validated-after-edit", "validated_after_edit"),
            ("verbatim retries", "verbatim_retries"),
            ("workarounds", "workarounds"),
        ):
            if f.get(key):
                parts.append(f"{label} {f[key]}")
        lines.append("      outcomes: " + " · ".join(parts))
        if f.get("expired"):
            lines.append(
                f"      censored: {f['expired']} expired_unresolved "
                "(excluded from all rate denominators)"
            )
        if f.get("hinted"):
            lines.append(
                f"      retrieval landing rate: {f['hinted_landed']}/"
                f"{f['hinted_resolved']} resolved opportunities"
            )
        if f.get("transitions"):
            lines.append(
                "      transitions: "
                + " · ".join(f"{k}×{v}" for k, v in f["transitions"].items())
            )
    est = iv.get("estimates")
    if est:
        lines.append("  estimated downstream cost (labeled estimates):")
        lines.append(
            f"    avoided reexecutions (estimate): {est['avoided_reexecutions']}"
        )
        lines.append(f"    avoided turns (estimate): {est['avoided_turns']}")
        if "avoided_runtime_s" in est:
            lines.append(
                f"    avoided runtime (estimate): {est['avoided_runtime_s']}s"
            )
        lines.append(
            "    note: avoided reexecutions = adapted-plan (dense/bypass) "
            "interventions resolved without an equivalent/slicer rerun; "
            "avoided turns = same count (one turn per avoided rerun); "
            "avoided runtime = sum of median observed same-signature "
            "runtimes over those interventions (omitted when unobserved); "
            "expired_unresolved is censored — excluded from every "
            "denominator"
        )
    lines.append("  evidence coverage:")
    lines.append("    family        events  required%    named  addressable")
    for fam, f in iv["families"].items():
        req = f"{f['required_pct']:.1f}" if f.get("required_pct") is not None else "—"
        named = f.get("named") or "—"
        addressable = f.get("addressable") or "—"
        lines.append(
            f"    {fam:<12}  {f['events']:>6}  {req:>9}  {named:>7}  "
            f"{addressable:>11}"
        )
    episodes = iv.get("episodes") or []
    if episodes:
        lines.append("  episodes:")
        for ep in episodes[:8]:
            seq = ep["first_seq"] if ep.get("first_seq") is not None else "?"
            gen = (
                f" (gen {ep['generation']})"
                if ep.get("generation") is not None else ""
            )
            lines.append(
                f"    '{ep['signature']}': first at seq {seq}{gen} · "
                f"response {'→'.join(ep['responses'])} · "
                f"outcomes {' → '.join(ep['outcomes'])}"
            )
        if len(episodes) > 8:
            lines.append(f"    … and {len(episodes) - 8} more signatures")
    return lines


def summary_line(sc: dict) -> str:
    """One-liner for wrap's session-end stderr note."""
    line = (
        f"ctx scorecard: {sc.get('rounds', sc['requests'])} rounds · "
        f"est ${sc['est_cost_usd']:.2f} · "
        f"out {sc['tokens']['output']:,} tok ({sc['output_per_request']}/req) · "
        f"cache hit {sc['cache_hit_pct']}% · invalidations {sc['invalidations']}"
        + (f" · cold-prefix {sc['cold_prefix_tok']:,}" if sc["cold_prefix_tok"] else "")
    )
    rr = sc.get("rescue_recovery")
    if rr:
        line += (
            f" · rescue@r{rr['first_rescued_round']} "
            f"(+{rr['rounds_after']} rounds after)"
        )
    d = sc.get("deliverable")
    if d:
        line += (
            f" · Δcode +{d['insertions']}/-{d['deletions']} in "
            f"{d['files_changed']}+{d['files_new']} files"
        )
    an = sc.get("anomalies")
    if an and an.get("starvation"):
        # Flagged only when starvation happened — landings alone are the
        # system working and earn no warning glyph.
        line += f" · ⚠ {an['starvation']} starvation/{an['landings']} landings"
    return line


def attach_deliverable(sc: dict, workspace_root: Path) -> dict:
    """Deliverable-level effort metrics (the ponytail lesson: measure the
    artifact, not just the wire). LOC delta and files touched from git —
    together with edit_share this makes both over-engineering and
    effort-thinning measurable regressions. Fail-open: metrics are absent
    rather than wrong when git is unavailable."""
    import subprocess

    try:
        num = subprocess.run(
            ["git", "diff", "HEAD", "--numstat"],
            cwd=workspace_root, capture_output=True, text=True, timeout=20,
        )
        ins = dels = files = 0
        if num.returncode == 0:
            for line in num.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    files += 1
                    if parts[0].isdigit():
                        ins += int(parts[0])
                    if parts[1].isdigit():
                        dels += int(parts[1])
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_root, capture_output=True, timeout=20,
        )
        # One parser (ctx.gitstatus): git quotes any path with a space or a
        # non-ASCII byte, so the old `ln[3:]` counted such files as new but
        # then failed to open them — their lines never reached lines_new.
        untracked_paths = (
            changed_paths(status.stdout, untracked_only=True)
            if status.returncode == 0
            else []
        )
        # Creation tasks do all their work in untracked files; count their
        # lines too (bounded: text-decodable, first 200 files).
        lines_new = 0
        for rel in untracked_paths[:200]:
            p = Path(workspace_root) / rel
            try:
                if p.is_file() and p.stat().st_size < 1_048_576:
                    lines_new += len(
                        p.read_bytes().decode("utf-8").splitlines()
                    )
            except (OSError, UnicodeDecodeError):
                continue
        sc["deliverable"] = {
            "insertions": ins,
            "deletions": dels,
            "files_changed": files,
            "files_new": len(untracked_paths),
            "lines_new": lines_new,
        }
    except Exception:
        pass
    return sc


def append_history(workspace_root: Path, sc: dict) -> None:
    """Accumulate scorecards for the policy-epoch learner. Fail-open."""
    try:
        path = session_reads_path(workspace_root, "scorecards.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sc, sort_keys=True) + "\n")
    except Exception:
        pass
