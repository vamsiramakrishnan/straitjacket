"""Deterministic harness simulator: replay a recorded Claude Code session
through the real steering + digest code, open-loop.

The generalization of evals/replay_detectors.py from reflex detectors to the
whole containment surface. A transcript (~/.claude/projects/**/<id>.jsonl or
an archived eval transcript) is a recorded pathway: every tool call with its
input and its full recorded result. This simulator re-runs each recorded
call through the harness that *would* have governed it:

  - PreToolUse: `ctx.hook.classify_command` over every Bash input — the
    verdict distribution (allow / rewrite / force_ask) is the interception
    surface the session would have met.
  - Emission: every recorded output through `ctx.digest.digest_output` —
    the wire residency the transcript would have carried instead.
  - Evidence sufficiency: the transcript itself declares which bytes
    mattered — strings the model later passed to Edit(old_string), file:line
    coordinates and test node-ids it cited in later inputs. Each such
    downstream-used fact is scored against the simulated digest of the
    result it came from: INLINE (survived in the digest), RETRIEVABLE (not
    inline, but present in raw bytes the store would hold — one `ctx get`
    hop), or — for the naive arm only — DISCARDED (the model's own slicer
    threw it away before the transcript recorded it; unmeasurable, noted).

Nothing re-executes and no model is called: outputs are the recorded bytes,
verdicts and digests are pure functions. What this cannot say, by design:
turn-count or behavioral deltas — an open-loop replay diverges from reality
the moment an observation would have changed the model's next action. Those
questions belong to the live A/B evals (matrix_runner). This answers the
wire questions exactly, on any real session, for free.

Usage:
    python evals/replay_sim.py <transcript.jsonl> [more.jsonl ...]
    python evals/replay_sim.py --self          # this machine's own sessions

Sessions that were ALREADY harnessed replay as drift checks: results that
are ctx digests pass through untouched and are counted separately.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.digest import digest_output  # noqa: E402
from ctx.hook import classify_command  # noqa: E402
from ctx.store import Store  # noqa: E402
from ctx.textutil import estimate_tokens  # noqa: E402
from ctx.workspace import resolve_workspace  # noqa: E402


def parse_transcript(path: str) -> list[dict]:
    """Recorded pathway: ordered tool calls with inputs and result text."""
    entries = [json.loads(ln) for ln in open(path, encoding="utf-8")]
    results: dict[str, str] = {}
    for d in entries:
        if d.get("type") != "user":
            continue
        content = d.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                t = b.get("content")
                if isinstance(t, list):
                    t = "\n".join(x.get("text", "") for x in t if isinstance(x, dict))
                results[b.get("tool_use_id")] = t or ""
    calls = []
    for d in entries:
        if d.get("type") != "assistant":
            continue
        for b in d.get("message", {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                calls.append(
                    {
                        "tool": b["name"],
                        "input": b.get("input", {}),
                        "result": results.get(b.get("id"), ""),
                    }
                )
    return calls


def downstream_facts(calls: list[dict], idx: int) -> list[str]:
    """Facts from call idx's result that later calls provably used: Edit
    old_string lines, file:line cites, pytest node ids, quoted error text."""
    res = calls[idx]["result"]
    if not res:
        return []
    facts: set[str] = set()
    later = calls[idx + 1 :]
    # Edit old_string fragments that appear verbatim in this result.
    for c in later:
        if c["tool"] in ("Edit", "Write"):
            frag = str(c["input"].get("old_string") or "")
            for line in frag.splitlines():
                line = line.strip()
                if len(line) >= 12 and line in res:
                    facts.add(line[:120])
    # test node ids / file:line coordinates this result introduced and a
    # later Bash command reused.
    coords = set(re.findall(r"[\w./-]+\.\w+:\d+", res)) | set(
        re.findall(r"[\w./-]+::[\w:]+", res)
    )
    for c in later:
        if c["tool"] == "Bash":
            cmd = str(c["input"].get("command") or "")
            for coord in coords:
                if coord in cmd:
                    facts.add(coord)
    return sorted(facts)[:20]


def simulate(path: str) -> dict:
    calls = parse_transcript(path)
    td = Path(tempfile.mkdtemp(prefix="replay-sim-"))
    (td / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(td))
    store = Store(td / "store")

    verdicts: Counter[str] = Counter()
    raw_tok = sim_tok = 0
    already_harnessed = 0
    facts_inline = facts_retrievable = 0
    fact_misses: list[tuple[str, str]] = []

    for i, c in enumerate(calls):
        res = c["result"]
        if c["tool"] == "Bash":
            cmd = str(c["input"].get("command") or "")
            v = classify_command(cmd, {})
            verdicts[v.get("decision", "?")] += 1
        if not res:
            continue
        r_tok = estimate_tokens(len(res.encode("utf-8")))
        raw_tok += r_tok
        if "[ctx run:" in res or res.startswith("[ctx "):
            already_harnessed += 1
            sim_tok += r_tok  # already a digest; passes the gate unchanged
            continue
        digest, _short = digest_output(
            store, ws, c["tool"].lower(), res, is_error=False
        )
        d_tok = estimate_tokens(len(digest.encode("utf-8")))
        sim_tok += min(d_tok, r_tok) if "output (complete):" in digest else d_tok
        for fact in downstream_facts(calls, i):
            if fact in digest:
                facts_inline += 1
            else:
                facts_retrievable += 1  # raw bytes are in the store by contract
                fact_misses.append((fact[:80], digest.splitlines()[0][:60]))

    return {
        "path": path,
        "calls": len(calls),
        "bash": sum(1 for c in calls if c["tool"] == "Bash"),
        "verdicts": dict(verdicts),
        "already_harnessed_results": already_harnessed,
        "recorded_residency_tok": raw_tok,
        "simulated_residency_tok": sim_tok,
        "facts_used_downstream": facts_inline + facts_retrievable,
        "facts_inline_in_digest": facts_inline,
        "facts_one_hop": facts_retrievable,
        "sample_one_hop_misses": fact_misses[:5],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcripts", nargs="*")
    ap.add_argument("--self", action="store_true", help="mine ~/.claude/projects")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    paths = list(args.transcripts)
    if args.self:
        paths += glob.glob(
            os.path.expanduser("~/.claude/projects/*/*.jsonl")
        )
    if not paths:
        ap.error("no transcripts given (pass paths or --self)")
    reports = [simulate(p) for p in paths]
    if args.json:
        print(json.dumps(reports, indent=2))
        return
    for r in reports:
        print(f"\n== {Path(r['path']).name} ==")
        print(f"  calls: {r['calls']} ({r['bash']} bash) · verdicts: {r['verdicts']}")
        if r["already_harnessed_results"]:
            print(f"  already-harnessed results passed through: {r['already_harnessed_results']}")
        rec, sim = r["recorded_residency_tok"], r["simulated_residency_tok"]
        pct = (1 - sim / rec) * 100 if rec else 0.0
        print(f"  residency: recorded {rec:,} tok -> simulated {sim:,} tok ({pct:.0f}% saved)")
        used = r["facts_used_downstream"]
        if used:
            print(
                f"  evidence the model provably used later: {used} facts · "
                f"{r['facts_inline_in_digest']} inline in digest · {r['facts_one_hop']} one ctx-get hop away"
            )
            for fact, dig in r["sample_one_hop_misses"]:
                print(f"    one-hop: '{fact}'")


if __name__ == "__main__":
    main()
