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

# Published per-MTok prices for decomposition estimates only.
PRICES = {
    "opus": {"in": 15.0, "out": 75.0, "write": 18.75, "read": 1.50},
    "sonnet": {"in": 3.0, "out": 15.0, "write": 3.75, "read": 0.30},
    "haiku": {"in": 1.0, "out": 5.0, "write": 1.25, "read": 0.10},
}

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "write_to_file", "replace_file_content"}


def _price_key(model_id: str) -> str:
    for key in PRICES:
        if key in model_id:
            return key
    return "sonnet"


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
        p = PRICES[_price_key(model)]
        est_cost += (
            u_in * p["in"] + u_read * p["read"] + u_cre * p["write"] + u_out * p["out"]
        ) / 1e6
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
            if u_read and u_read < thread["max_read"]:
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
    return "\n".join(lines)


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
            cwd=workspace_root, capture_output=True, text=True, timeout=20,
        )
        untracked_paths = [
            ln[3:] for ln in status.stdout.splitlines() if ln.startswith("??")
        ] if status.returncode == 0 else []
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
        path = Path(workspace_root) / ".ctx-session-reads" / "scorecards.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sc, sort_keys=True) + "\n")
    except Exception:
        pass
