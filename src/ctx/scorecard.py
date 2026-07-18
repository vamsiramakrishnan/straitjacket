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


def compute_scorecard(proxy_state_dir: Path) -> dict | None:
    """Fold wire.jsonl into a scorecard dict. None when no observations."""
    wire = Path(proxy_state_dir) / "wire.jsonl"
    if not wire.is_file():
        return None
    tok = {"input": 0, "read": 0, "creation": 0, "output": 0}
    ms = {"connect": 0.0, "ttfb": 0.0, "gen": 0.0}
    tools: dict[str, int] = {}
    per_model: dict[str, dict[str, int]] = {}
    requests = invalidations = 0
    cold_prefix = 0
    max_read = 0
    est_cost = 0.0
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
        if u_read == 0 and u_cre > 4096 and cold_prefix == 0:
            cold_prefix = u_cre
        elif u_read and u_read < max_read and rec.get("messages", 0) > 1:
            invalidations += 1
        max_read = max(max_read, u_read)
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
    return {
        "schema": "ctx.scorecard/v1",
        "requests": requests,
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
    return "\n".join(lines)


def summary_line(sc: dict) -> str:
    """One-liner for wrap's session-end stderr note."""
    line = (
        f"ctx scorecard: {sc['requests']} req · est ${sc['est_cost_usd']:.2f} · "
        f"out {sc['tokens']['output']:,} tok ({sc['output_per_request']}/req) · "
        f"cache hit {sc['cache_hit_pct']}% · invalidations {sc['invalidations']}"
        + (f" · cold-prefix {sc['cold_prefix_tok']:,}" if sc["cold_prefix_tok"] else "")
    )
    d = sc.get("deliverable")
    if d:
        line += (
            f" · Δcode +{d['insertions']}/-{d['deletions']} in "
            f"{d['files_changed']}+{d['files_new']} files"
        )
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
        untracked = sum(
            1 for ln in status.stdout.splitlines() if ln.startswith("??")
        ) if status.returncode == 0 else 0
        sc["deliverable"] = {
            "insertions": ins,
            "deletions": dels,
            "files_changed": files,
            "files_new": untracked,
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
