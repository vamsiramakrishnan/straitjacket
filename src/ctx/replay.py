"""Session-history replay: the deterministic learning loop over recorded
Claude Code transcripts (ROADMAP M-F).

Every Claude Code host journals sessions as JSONL under
``~/.claude/projects/<encoded-project>/<session>.jsonl`` — inputs, tool
calls, and full tool results. That store is a goldmine of ground truth this
harness can learn from without running a single command or model: replay
each recorded call through the *real* steering and digest code, open-loop,
and score what the harness would have done.

Three questions, answered exactly:

- **Interception**: `classify_command` verdict per recorded Bash input.
- **Residency**: recorded result bytes vs the digest the emission layer
  would have produced from the same bytes.
- **Evidence sufficiency**: the transcript proves which bytes the model
  used downstream (fragments later passed to Edit ``old_string``,
  file:line coordinates and test node-ids reused in later commands); each
  such fact is scored inline-in-digest or one-``ctx get``-hop away. On
  already-harnessed transcripts this doubles as a regression gate: digests
  a model actually worked from must keep their sufficiency after profile
  changes.

- **Gaps** (``--gaps``): where the raw bytes fell — which profiles claimed
  them, how much fell to ``text/v1``, hand-rolled slicer frequency, and
  ``ctx eval`` opportunities — the empirical priority list for the next
  coverage wave, mined from real sessions instead of intuition.

- **Evidence regret** (``--regret``): the rate–distortion frontier gap per
  profile (docs/THEORY.md). For each facts-bearing call, ``oracle`` is the
  token size of exactly the downstream-used facts (a *lower* bound on the
  true minimal sufficient statistic, since the trajectory only proves a
  subset of what was needed), ``actual`` is what the harness charged for
  that evidence (digest tokens + a deterministic ``ctx get`` hop price per
  one-hop fact), and ``R = actual − oracle``. Because the oracle proxy is
  a lower bound, measured R is an **upper bound on the true gap** — the
  metric can under-flatter but never over-flatter. Calls with no provably
  used facts are reported as *unattributed digest spend*, never folded
  into R: the proxy is blind to conclusion-shaped evidence ("all tests
  passed"), and charging those calls with a zero oracle would drown the
  signal. ``naive regret`` = raw tokens − oracle on the same calls, so
  each profile's row shows both the realized saving and what is still on
  the table.

Open-loop limits, stated plainly: no turn-count or behavioral deltas — the
recorded trajectory stops being ground truth at the first observation that
would have changed the model's next action. Those questions belong to the
live A/B evals. Replay is read-only: simulation uses a throwaway workspace
and store; nothing touches the caller's artifact store, and printed fact
samples pass through the workspace redaction patterns.
"""

from __future__ import annotations

import glob
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

_SLICER_RE = re.compile(r"\|\s*(head|tail|grep|sed|awk|cut|wc)\b")
_ENV_PREFIX_RE = re.compile(r"^(?:\w+=\S*\s+)*")

# Deterministic hop-price model for one-hop facts (docs/THEORY.md §regret):
# recovering an omitted fact costs one `ctx get --lines A:B` round — a fixed
# command/header scaffold plus the returned slice (the fact's line ± context).
# Declared constants, not measurements, so replay stays byte-deterministic.
_HOP_SCAFFOLD_TOK = 20
_HOP_CONTEXT_LINES = 2


def _hop_cost(fact: str, raw: str | None) -> int:
    """Tokens one `ctx get` hop pays to recover ``fact``.

    With the raw bytes at hand, price the actual ±2-line slice around the
    fact's first occurrence. Without them (already-harnessed results, where
    replay holds only the digest and the store contract owns the raw), price
    the floor — scaffold + the fact line itself. The floor direction is
    declared: it can understate ``actual`` there by a few context lines."""
    from ctx.textutil import estimate_tokens

    if raw is not None:
        pos = raw.find(fact)
        if pos >= 0:
            lines = raw.splitlines()
            at = raw[:pos].count("\n")
            lo = max(0, at - _HOP_CONTEXT_LINES)
            hi = min(len(lines), at + _HOP_CONTEXT_LINES + 1)
            window = "\n".join(lines[lo:hi])
            return _HOP_SCAFFOLD_TOK + estimate_tokens(len(window.encode("utf-8")))
    return _HOP_SCAFFOLD_TOK + estimate_tokens(len(fact.encode("utf-8")))


def _argv_of(call: dict[str, Any]) -> list[str] | None:
    """Best-effort argv of a recorded Bash command: env-prefix stripped,
    first pipe segment, shell-split. None when unparseable — dispatch then
    falls back to output shape, never crashes the replay."""
    import shlex

    cmd = str(call["input"].get("command") or "").strip()
    if not cmd:
        return None
    first = _ENV_PREFIX_RE.sub("", cmd.split("|", 1)[0].split("&&", 1)[0].strip())
    try:
        argv = shlex.split(first)
    except ValueError:
        argv = first.split()
    return argv or None
_EVAL_OPP_RE = re.compile(r"<<|python3? -c |python3? - ")
_COORD_RE = re.compile(r"[\w./-]+\.\w+:\d+")
_NODEID_RE = re.compile(r"[\w./-]+::[\w:]+")


def default_history_paths() -> list[str]:
    return sorted(
        glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    )


def parse_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Recorded pathway: ordered tool calls with inputs and result text."""
    entries = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            try:
                entries.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # partial trailing line in a live session file
    results: dict[str, tuple[str, bool]] = {}
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
                results[b.get("tool_use_id")] = (t or "", bool(b.get("is_error")))
    calls: list[dict[str, Any]] = []
    for d in entries:
        if d.get("type") != "assistant":
            continue
        for b in d.get("message", {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                res, is_err = results.get(b.get("id"), ("", False))
                calls.append(
                    {
                        "tool": b["name"],
                        "input": b.get("input", {}),
                        "result": res,
                        "is_error": is_err,
                    }
                )
    return calls


def downstream_facts(calls: list[dict[str, Any]], idx: int) -> list[str]:
    """Facts from call idx's result that later calls provably used."""
    res = calls[idx]["result"]
    if not res:
        return []
    facts: set[str] = set()
    later = calls[idx + 1 :]
    for c in later:
        if c["tool"] in ("Edit", "Write"):
            frag = str(c["input"].get("old_string") or "")
            for line in frag.splitlines():
                line = line.strip()
                if len(line) >= 12 and line in res:
                    facts.add(line[:120])
    coords = set(_COORD_RE.findall(res)) | set(_NODEID_RE.findall(res))
    for c in later:
        if c["tool"] == "Bash":
            cmd = str(c["input"].get("command") or "")
            for coord in coords:
                if coord in cmd:
                    facts.add(coord)
    return sorted(facts)[:20]


def simulate_session(path: str | Path) -> dict[str, Any]:
    """Open-loop replay of one transcript through steering + digest code.

    Uses a throwaway workspace/store so replay never writes into the
    caller's artifact store.
    """
    from ctx.digest import digest_output
    from ctx.hook import classify_command
    from ctx.store import Store
    from ctx.textutil import estimate_tokens
    from ctx.workspace import resolve_workspace

    calls = parse_transcript(path)
    td = Path(tempfile.mkdtemp(prefix="ctx-replay-"))
    (td / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(td))
    store = Store(td / "store")

    verdicts: Counter[str] = Counter()
    programs: Counter[str] = Counter()
    profile_tok: Counter[str] = Counter()
    raw_tok = sim_tok = 0
    already_harnessed = slicers = eval_opps = 0
    facts_inline = facts_hop = 0
    misses: list[str] = []

    # Evidence-regret ledger (docs/THEORY.md): per-profile, facts-bearing
    # calls only. Fact-free calls land in the unattributed bucket instead —
    # the facts proxy cannot see conclusion-shaped evidence, and a zero
    # oracle there would drown the signal in false regret.
    regret_by_profile: dict[str, dict[str, int]] = {}
    unattributed_tok = unattributed_calls = 0

    def _tally_regret(
        prof: str,
        facts: list[str],
        digest: str,
        digest_tok: int,
        raw_text: str | None,
    ) -> None:
        """``raw_text`` is the recorded raw output when replay holds it
        (simulated and read-path calls). For already-harnessed calls it is
        None: the raw stayed in the store, so the naive counterfactual is
        unknowable and those calls are excluded from naive-R — comparing a
        digest to itself would fake a closed gap of zero."""
        nonlocal unattributed_tok, unattributed_calls
        if not facts:
            unattributed_tok += digest_tok
            unattributed_calls += 1
            return
        b = regret_by_profile.setdefault(
            prof,
            {"calls": 0, "facts": 0, "inline": 0, "hops": 0,
             "oracle_tok": 0, "actual_tok": 0,
             "naive_calls": 0, "naive_tok": 0, "naive_oracle_tok": 0,
             "known_actual_tok": 0},
        )
        b["calls"] += 1
        actual = digest_tok
        call_oracle = 0
        for fact in facts:
            b["facts"] += 1
            call_oracle += estimate_tokens(len(fact.encode("utf-8")))
            if fact in digest:
                b["inline"] += 1
            else:
                b["hops"] += 1
                actual += _hop_cost(fact, raw_text)
        b["oracle_tok"] += call_oracle
        b["actual_tok"] += actual
        if raw_text is not None:
            # The naive comparison must subtract like from like: naive, its
            # oracle, and the harness's own charge are all restricted to
            # this same raw-known population.
            b["naive_calls"] += 1
            b["naive_tok"] += estimate_tokens(len(raw_text.encode("utf-8")))
            b["naive_oracle_tok"] += call_oracle
            b["known_actual_tok"] += actual

    for i, c in enumerate(calls):
        res = c["result"]
        if c["tool"] == "Bash":
            cmd = str(c["input"].get("command") or "")
            verdicts[classify_command(cmd, {}).get("decision", "?")] += 1
            m = re.match(r"(?:\w+=\S+\s+)*(?:cd \S+ && )?(\S+)", cmd)
            if m:
                programs[m.group(1).rsplit("/", 1)[-1]] += 1
            if _SLICER_RE.search(cmd):
                slicers += 1
            if _EVAL_OPP_RE.search(cmd):
                eval_opps += 1
        if not res:
            continue
        r_tok = estimate_tokens(len(res.encode("utf-8")))
        raw_tok += r_tok
        if "[ctx run:" in res or res.startswith("[ctx "):
            # Already a ctx digest: passes the gate unchanged — but its
            # evidence sufficiency IS scorable (this is the regression gate
            # on sessions the harness actually served): a downstream-used
            # fact is inline if the digest the model saw carried it, and
            # one-hop otherwise (raw bytes are in the store by contract).
            already_harnessed += 1
            sim_tok += r_tok
            facts = downstream_facts(calls, i)
            for fact in facts:
                if fact in res:
                    facts_inline += 1
                else:
                    facts_hop += 1
                    misses.append(fact[:80])
            prof = (
                res.split("profile=", 1)[1].split("]", 1)[0]
                if "profile=" in res
                else "harnessed"
            )
            # digest IS the recorded result; raw bytes live in the store, so
            # hop pricing falls back to its declared floor (raw_text=None).
            _tally_regret(prof, facts, res, r_tok, None)
            continue
        if c["tool"] not in ("Bash",) and not c["tool"].startswith("mcp__"):
            # Read/Grep/Glob run under the read path (budgets, native caps),
            # not the emission gate — shape-digesting source-file Reads here
            # would misclaim (a file that *contains* test markers is not a
            # test run). Count them raw under their own bucket. For regret
            # the read path delivers everything inline (digest == raw), so
            # its row honestly shows regret == naive regret: the whole read
            # channel is un-collapsed evidence, priced as such.
            sim_tok += r_tok
            profile_tok["read-path"] += r_tok
            _tally_regret("read-path", downstream_facts(calls, i), res, r_tok, res)
            continue
        # Command-anchored detection: the real steering path hands ctx run
        # the full argv, so replay must too — shape-only dispatch would
        # misattribute e.g. quiet go-test output to text/v1.
        argv = _argv_of(c) if c["tool"] == "Bash" else None
        digest, _short = digest_output(
            store, ws, c["tool"].lower(), res, is_error=c.get("is_error", False),
            argv=argv,
        )
        d_tok = estimate_tokens(len(digest.encode("utf-8")))
        inline = "output (complete):" in digest
        sim_tok += min(d_tok, r_tok) if inline else d_tok
        prof = digest.split("profile=", 1)[1].split("]", 1)[0] if "profile=" in digest else "?"
        profile_tok[("inline" if inline else prof)] += r_tok
        facts = downstream_facts(calls, i)
        for fact in facts:
            if fact in digest:
                facts_inline += 1
            else:
                facts_hop += 1
                misses.append(fact[:80])
        _tally_regret(
            "inline" if inline else prof,
            facts,
            digest,
            min(d_tok, r_tok) if inline else d_tok,
            res,
        )

    from ctx.textutil import sanitize_for_model

    safe_misses = [sanitize_for_model(m, ws.config.redaction.patterns)[0] for m in misses[:5]]
    for b in regret_by_profile.values():
        b["regret_tok"] = b["actual_tok"] - b["oracle_tok"]
        # Naive comparison only over calls whose raw bytes replay actually
        # holds (harnessed calls are excluded — their counterfactual is
        # unknowable from the transcript alone).
        b["naive_regret_tok"] = b["naive_tok"] - b["naive_oracle_tok"]
    return {
        "path": str(path),
        "calls": len(calls),
        "bash": sum(1 for c in calls if c["tool"] == "Bash"),
        "verdicts": dict(sorted(verdicts.items())),
        "programs": dict(programs.most_common(10)),
        "slicer_commands": slicers,
        "eval_opportunities": eval_opps,
        "already_harnessed_results": already_harnessed,
        "recorded_residency_tok": raw_tok,
        "simulated_residency_tok": sim_tok,
        "raw_tok_by_profile": dict(sorted(profile_tok.items(), key=lambda kv: -kv[1])),
        "facts_used_downstream": facts_inline + facts_hop,
        "facts_inline_in_digest": facts_inline,
        "facts_one_hop": facts_hop,
        "sample_one_hop": safe_misses,
        "regret_by_profile": {k: dict(v) for k, v in sorted(regret_by_profile.items())},
        "unattributed_digest_tok": unattributed_tok,
        "unattributed_calls": unattributed_calls,
    }


def render_report(reports: list[dict[str, Any]], *, gaps: bool = False) -> str:
    out: list[str] = []
    for r in reports:
        out.append(f"== {Path(r['path']).name} ==")
        out.append(
            f"  calls: {r['calls']} ({r['bash']} bash) · verdicts: {r['verdicts']}"
        )
        if r["already_harnessed_results"]:
            out.append(
                f"  already-harnessed results passed through: {r['already_harnessed_results']}"
            )
        rec, sim = r["recorded_residency_tok"], r["simulated_residency_tok"]
        pct = (1 - sim / rec) * 100 if rec else 0.0
        out.append(
            f"  residency: recorded {rec:,} tok -> simulated {sim:,} tok ({pct:.0f}% saved)"
        )
        used = r["facts_used_downstream"]
        if used:
            out.append(
                f"  downstream-used evidence: {used} facts · "
                f"{r['facts_inline_in_digest']} inline · {r['facts_one_hop']} one-hop"
            )
            for m in r["sample_one_hop"]:
                out.append(f"    one-hop: '{m}'")
        out.append("")
    if gaps and reports:
        prog: Counter[str] = Counter()
        prof: Counter[str] = Counter()
        slicers = opps = bash = 0
        for r in reports:
            prog.update(r["programs"])
            prof.update(r["raw_tok_by_profile"])
            slicers += r["slicer_commands"]
            opps += r["eval_opportunities"]
            bash += r["bash"]
        out.append("== gaps (aggregate) ==")
        out.append("  bash programs: " + " · ".join(f"{k} {v}" for k, v in prog.most_common(12)))
        out.append(
            "  raw tokens by claiming profile: "
            + " · ".join(f"{k} {v:,}" for k, v in prof.most_common(8))
        )
        out.append(
            f"  hand-rolled slicers: {slicers}/{bash} bash commands · "
            f"ctx-eval opportunities: {opps}"
        )
        out.append(
            "  reading: raw tokens under text/v1 with downstream one-hop facts "
            "mark the next profile to build; slicer-heavy programs mark digests "
            "the model routes around."
        )
    return "\n".join(out).rstrip()


def render_regret(reports: list[dict[str, Any]]) -> str:
    """The evidence-regret scoreboard: per-profile frontier gap, aggregated
    across sessions (docs/THEORY.md). ``frontier`` = oracle/actual ∈ (0, 1];
    1.00 means the digest delivered exactly the used evidence and nothing
    else — the rate–distortion frontier under the facts proxy."""
    agg: dict[str, Counter[str]] = {}
    unattributed_tok = unattributed_calls = 0
    for r in reports:
        for prof, b in r.get("regret_by_profile", {}).items():
            agg.setdefault(prof, Counter()).update(b)
        unattributed_tok += r.get("unattributed_digest_tok", 0)
        unattributed_calls += r.get("unattributed_calls", 0)

    out = ["== evidence regret (R = actual − oracle · upper bound on the frontier gap) =="]
    if not agg:
        out.append("  no downstream-used facts found — nothing scoreable")
    else:
        out.append(
            f"  {'profile':<14} {'calls':>5} {'facts':>5} {'in':>4} {'hop':>4}"
            f" {'oracle':>8} {'actual':>8} {'R':>8} {'naive-R':>9} {'frontier':>9}"
        )
        tot: Counter[str] = Counter()
        for prof, b in sorted(agg.items(), key=lambda kv: -kv[1]["regret_tok"]):
            tot.update(b)
            frontier = b["oracle_tok"] / b["actual_tok"] if b["actual_tok"] else 1.0
            # naive-R is only meaningful where replay held the raw bytes;
            # already-harnessed calls have no knowable counterfactual.
            naive_col = f"{b['naive_regret_tok']:,}" if b["naive_calls"] else "—"
            out.append(
                f"  {prof:<14} {b['calls']:>5} {b['facts']:>5} {b['inline']:>4}"
                f" {b['hops']:>4} {b['oracle_tok']:>8,} {b['actual_tok']:>8,}"
                f" {b['regret_tok']:>8,} {naive_col:>9}"
                f" {frontier:>9.2f}"
            )
        out.append(
            f"  totals: R {tot['regret_tok']:,} tok · frontier "
            f"{(tot['oracle_tok'] / tot['actual_tok'] if tot['actual_tok'] else 1.0):.2f}"
        )
        if tot["naive_calls"]:
            # Same-population comparison: naive-R and the harness R both
            # restricted to the raw-known calls (harnessed calls have no
            # knowable counterfactual and are excluded from BOTH sides).
            naive_r = tot["naive_tok"] - tot["naive_oracle_tok"]
            harness_r = tot["known_actual_tok"] - tot["naive_oracle_tok"]
            closed = (1 - harness_r / naive_r) * 100 if naive_r > 0 else 0.0
            out.append(
                f"  naive comparison ({tot['naive_calls']} raw-known calls): "
                f"naive-R {naive_r:,} tok vs R {harness_r:,} — "
                f"{closed:.0f}% of the naive gap closed"
            )
    if unattributed_calls:
        out.append(
            f"  unattributed digest spend: {unattributed_tok:,} tok over "
            f"{unattributed_calls} fact-free calls (facts proxy is blind to "
            "conclusion-shaped evidence; excluded from R by design)"
        )
    out.append(
        "  reading: R is an UPPER bound — the facts oracle is a lower bound on "
        "true sufficiency. A profile with persistent R needs inlining (hops>0) "
        "or a slimmer digest (oracle ≪ actual with facts inline)."
    )
    return "\n".join(out)
