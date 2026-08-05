#!/usr/bin/env python3
"""Seven containment strategies, one task — model-free needle survival.

Every arm is handed the *same bytes* — a 20,001-line integration log hiding one
structurally rare "quiet needle" (no ERROR/fail keyword) plus two loud ERRORs —
and we measure, with one shared tokenizer, what the model would end up seeing.

The arms represent the strategies in README's field comparison. Headroom and
straitjacket execute their real implementations. The other five arms are
explicit, inspectable models of their documented strategy rather than claims
to execute those third-party products:

  naive     — the flood passes through verbatim (no harness).
  caveman   — terse "say less": head+tail truncation, middle dropped.
  rtk       — bash-hook flood filter: keep loud errors + context, drop the
              success-path bulk (lossy on success, no addresses).
  ponytail  — advisory ruleset injected; the bytes still pass through raw.
  maki      — sandboxed script collapses N ops into a tiny result; the script
              and full output vanish (no provenance, no addresses).
  headroom  — headroom-ai wire-proxy compression (real library).
  sj        — `ctx run` birth-gate capture → bounded digest + retrieval address.

No language model is involved: this exercises the delivery/compression layer
only, so it is deterministic and cheap. Three metrics per arm:
  * out tokens the model would see (shared o200k_base tokenizer);
  * did the quiet needle survive;
  * does an omitted line keep a resolvable retrieval address.

Usage:
    pip install -e '.[dev]' headroom-ai tiktoken
    python evals/field_needle.py            # human table
    python evals/field_needle.py --json      # machine record
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

QUIET_NEEDLE_LINE = 14238
QUIET_NEEDLE_MARK = "fell back to legacy gateway after circuit opened"
LOUD_NEEDLE_MARK = "cache eviction storm"
TOTAL_LINES = 20001
SEED = 1234

# Caveman keeps this many head + tail lines (the "say less" truncation).
CAVEMAN_HEAD = 40
CAVEMAN_TAIL = 40

# Ponytail's injected discipline ladder (advisory only — the bytes still flow).
PONYTAIL_RULESET = """\
[discipline ladder]
1. Prefer narrow reads over full dumps.
2. Summarise; do not restate raw output.
3. Cite line numbers instead of quoting.
4. Escalate retrieval only when evidence is insufficient.
5. Keep narration terse.
(advisory: the agent is asked to comply; nothing enforces it.)
"""


def build_corpus() -> str:
    """Deterministic hostile log: 20k INFO lines, 1 quiet needle, 2 loud ERRORs."""
    rng = random.Random(SEED)
    workers = [f"worker-{i}" for i in range(1, 33)]
    verbs = ["accepted", "processed", "forwarded", "cached",
             "validated", "dispatched", "completed", "enqueued"]
    out = []
    for i in range(1, TOTAL_LINES + 1):
        if i == QUIET_NEEDLE_LINE:
            out.append(f"INFO {rng.choice(workers)} checkout request req-14237 "
                       "fell back to legacy gateway after circuit opened")
        elif i == 5000:
            out.append("ERROR db-pool connection refused on shard 7 (attempt 3)")
        elif i == 17650:
            out.append("ERROR cache eviction storm: 4128 keys dropped in 200ms window")
        else:
            out.append(f"INFO {rng.choice(workers)} request req-{i:05d} "
                       f"{rng.choice(verbs)} in {rng.randint(2, 90)}ms")
    return "\n".join(out) + "\n"


def token_counter():
    import tiktoken

    enc = tiktoken.get_encoding("o200k_base")
    return lambda text: len(enc.encode(text, disallowed_special=()))


def _flatten(messages) -> str:
    text = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            text.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    text.append(b.get("text", ""))
                    inner = b.get("content", "")
                    text.append(inner if isinstance(inner, str) else str(inner))
    return "\n".join(text)


def realistic_transcript(raw: str) -> list:
    msgs = [
        {"role": "user", "content": "Investigate the integration log; find anything anomalous."},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "cat corpus.log"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": raw}]},
    ]
    for k in range(5):
        msgs.append({"role": "assistant", "content": f"Step {k}: continuing the analysis."})
        msgs.append({"role": "user", "content": f"Understood, continue with step {k}."})
    return msgs


def _result(tool, text, count, *, address=None):
    return {
        "tool": tool,
        "out_tokens": count(text),
        "quiet_needle_survived": QUIET_NEEDLE_MARK in text,
        "loud_needle_survived": LOUD_NEEDLE_MARK in text,
        "retrieval_address": address,
        "output_excerpt": text[:400],
    }


def run_naive(raw: str, count) -> dict:
    # The flood enters the transcript verbatim — no harness at all.
    return _result("naive (raw flood)", raw, count, address=None)


def run_caveman(raw: str, count) -> dict:
    # Terse "say less": keep head + tail, drop the middle with a note.
    lines = raw.splitlines()
    kept = (lines[:CAVEMAN_HEAD]
            + [f"... [{len(lines) - CAVEMAN_HEAD - CAVEMAN_TAIL} lines truncated] ..."]
            + lines[-CAVEMAN_TAIL:])
    return _result("caveman (head+tail trunc)", "\n".join(kept) + "\n", count, address=None)


def run_ponytail(raw: str, count) -> dict:
    # Advisory ruleset prepended; the raw flood still passes through unchanged.
    return _result("ponytail (advisory rules)", PONYTAIL_RULESET + "\n" + raw, count,
                   address=None)


_LOUD_KEYWORDS = ("ERROR", "WARN", "FAIL", "EXCEPTION", "CRITICAL")


def run_rtk(raw: str, count) -> dict:
    # Fast bash-hook flood filter: keep lines matching a loud-error pattern
    # plus head/tail context, drop the success-path bulk. The quiet needle is
    # an INFO success line → filtered out. Truncated, no retrieval address.
    lines = raw.splitlines()
    keep = set(range(min(10, len(lines)))) | set(range(max(0, len(lines) - 10), len(lines)))
    keep |= {i for i, l in enumerate(lines) if any(k in l for k in _LOUD_KEYWORDS)}
    out, prev = [], -2
    for i in sorted(keep):
        if out and i != prev + 1:
            out.append(f"... [filtered {i - prev - 1} lines] ...")
        out.append(lines[i])
        prev = i
    return _result("rtk (bash-hook filter)", "\n".join(out) + "\n", count, address=None)


def run_maki(raw: str, count) -> dict:
    # A sandboxed script collapses the scan into a tiny aggregate: it greps for
    # anomalies and emits only its matches. The quiet needle carries no anomaly
    # keyword, so the script never selects it; the script and full log vanish
    # into the chat (no provenance, no retrieval address).
    lines = raw.splitlines()
    matches = [l for l in lines if any(k in l for k in ("ERROR", "FAIL", "EXCEPTION"))]
    text = (f"[maki: sandboxed script scanned {len(lines):,} lines for anomalies]\n"
            + "\n".join(matches)
            + f"\n[{len(matches)} anomalies found; script and full log not retained]")
    return _result("maki (sandboxed script)", text, count, address=None)


def run_headroom(raw: str, count) -> dict:
    try:
        import headroom
    except Exception as e:  # optional dependency
        return {"tool": "headroom (not installed)", "skipped": str(e),
                "out_tokens": None, "quiet_needle_survived": None,
                "loud_needle_survived": None, "retrieval_address": None}
    res = headroom.compress(realistic_transcript(raw), model_limit=200_000)
    text = _flatten(res.messages)
    out = _result(f"headroom-ai {getattr(headroom, '__version__', '?')}", text, count)
    out["transforms"] = list(getattr(res, "transforms_applied", []))
    return out


def run_sj(raw: str, count) -> dict:
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "corpus.log").write_text(raw, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "ctx", "run", "--shell", "cat corpus.log"],
            cwd=td, capture_output=True, text=True, check=True,
        )
    digest = proc.stdout
    has_addr = "ctx get run:" in digest and "--lines" in digest
    return _result("sj (ctx run logtemplate/v1)", digest, count, address=has_addr)


ARMS = [run_naive, run_caveman, run_rtk, run_ponytail, run_maki, run_headroom, run_sj]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    raw = build_corpus()
    count = token_counter()
    raw_tokens = count(raw)
    results = [fn(raw, count) for fn in ARMS]

    record = {
        "corpus": {"lines": TOTAL_LINES, "bytes": len(raw.encode("utf-8")),
                   "raw_tokens_o200k": raw_tokens, "quiet_needle_line": QUIET_NEEDLE_LINE},
        "arms": results,
    }
    if args.json:
        print(json.dumps(record, indent=2))
        return 0

    print(f"Task: find the anomaly in a {TOTAL_LINES:,}-line log "
          f"({len(raw.encode()):,} B · {raw_tokens:,} tok o200k_base)")
    print(f"Quiet needle at L{QUIET_NEEDLE_LINE}: {QUIET_NEEDLE_MARK!r}\n")
    hdr = f"{'arm':<30}{'out tok':>10}{'ratio':>9}{'quiet needle':>15}{'address':>10}"
    print(hdr)
    print("-" * len(hdr))
    for arm in results:
        if arm.get("out_tokens") is None:
            print(f"{arm['tool']:<30}{'—':>10}{'—':>9}{'—':>15}{'—':>10}")
            continue
        ratio = raw_tokens / arm["out_tokens"] if arm["out_tokens"] else float("inf")
        needle = "SURVIVED" if arm["quiet_needle_survived"] else "DROPPED"
        addr = ("yes" if arm["retrieval_address"] else
                "n/a" if arm["retrieval_address"] is None else "no")
        print(f"{arm['tool']:<30}{arm['out_tokens']:>10,}{ratio:>8.1f}×"
              f"{needle:>15}{addr:>10}")
    print("\nsurvived+addressable = the model can recover the needle on demand; "
          "\nsurvived+no-address = present but unciteable; DROPPED = gone with no trace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
