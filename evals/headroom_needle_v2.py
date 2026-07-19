#!/usr/bin/env python3
"""Model-free needle-survival head-to-head: Headroom vs ``ctx run`` (logtemplate).

Both systems are given the *identical bytes* — a 20,001-line integration log that
hides one structurally rare "quiet needle" (no ERROR/fail/exception keyword) plus
two loud ERROR lines. We measure, with one shared tokenizer:

  * output size (tokens the model would see);
  * whether the quiet needle survived;
  * whether an omitted line keeps a resolvable retrieval address.

No language model is involved: this exercises the compression/digest layer only,
so it is deterministic and cheap to re-run in CI or a review sandbox.

Usage:
    pip install -e '.[dev]' headroom-ai tiktoken
    python evals/headroom_needle_v2.py            # human table
    python evals/headroom_needle_v2.py --json      # machine record
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


def build_corpus() -> str:
    """Deterministic hostile log: 20k INFO lines, 1 quiet needle, 2 loud ERRORs."""
    rng = random.Random(SEED)
    workers = [f"worker-{i}" for i in range(1, 33)]
    verbs = [
        "accepted", "processed", "forwarded", "cached",
        "validated", "dispatched", "completed", "enqueued",
    ]
    out = []
    for i in range(1, TOTAL_LINES + 1):
        if i == QUIET_NEEDLE_LINE:
            out.append(
                f"INFO {rng.choice(workers)} checkout request req-14237 "
                "fell back to legacy gateway after circuit opened"
            )
        elif i == 5000:
            out.append("ERROR db-pool connection refused on shard 7 (attempt 3)")
        elif i == 17650:
            out.append("ERROR cache eviction storm: 4128 keys dropped in 200ms window")
        else:
            out.append(
                f"INFO {rng.choice(workers)} request req-{i:05d} "
                f"{rng.choice(verbs)} in {rng.randint(2, 90)}ms"
            )
    return "\n".join(out) + "\n"


def token_counter():
    """A single shared tokenizer for both arms (tokenizer-independent fairness)."""
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
    """The flood as it actually occurs: an older tool_result, newer turns after it.

    A lone most-recent message is shielded by Headroom's protect_recent default,
    which is not how a flood is encountered in a real agent loop. Here the log is
    the result of a bash tool call, followed by five more exchanges.
    """
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


def run_headroom(raw: str, count) -> dict:
    import headroom

    # Default out-of-the-box config; the flood is an older tool_result.
    res = headroom.compress(realistic_transcript(raw), model_limit=200_000)
    compressed_text = _flatten(res.messages)
    return {
        "tool": f"headroom-ai {getattr(headroom, '__version__', '?')}",
        "out_tokens": count(compressed_text),
        "self_reported_before": res.tokens_before,
        "self_reported_after": res.tokens_after,
        "transforms": list(res.transforms_applied),
        "quiet_needle_survived": QUIET_NEEDLE_MARK in compressed_text,
        "loud_needle_survived": LOUD_NEEDLE_MARK in compressed_text,
        "retrieval_address": None,
        "output_excerpt": compressed_text[:600],
    }


def run_ctx(raw: str, count, repo_root: Path) -> dict:
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "corpus.log"
        log.write_text(raw, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "ctx", "run", "--shell", "cat corpus.log"],
            cwd=td, capture_output=True, text=True, check=True,
        )
    digest = proc.stdout
    has_addr = "ctx get run:" in digest and "--lines" in digest
    return {
        "tool": "ctx run (logtemplate/v1)",
        "out_tokens": count(digest),
        "quiet_needle_survived": QUIET_NEEDLE_MARK in digest,
        "loud_needle_survived": LOUD_NEEDLE_MARK in digest,
        "retrieval_address": has_addr,
        "output_excerpt": digest[:600],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    raw = build_corpus()
    count = token_counter()
    raw_tokens = count(raw)

    headroom_res = run_headroom(raw, count)
    ctx_res = run_ctx(raw, count, repo_root)

    record = {
        "corpus": {
            "lines": TOTAL_LINES,
            "bytes": len(raw.encode("utf-8")),
            "raw_tokens_o200k": raw_tokens,
            "quiet_needle_line": QUIET_NEEDLE_LINE,
        },
        "arms": [headroom_res, ctx_res],
    }

    if args.json:
        print(json.dumps(record, indent=2))
        return 0

    print(f"Corpus: {TOTAL_LINES:,} lines · {len(raw.encode()):,} B · "
          f"{raw_tokens:,} tok (o200k_base)")
    print(f"Quiet needle at L{QUIET_NEEDLE_LINE}: {QUIET_NEEDLE_MARK!r}\n")
    hdr = f"{'arm':<28}{'out tok':>9}{'ratio':>8}{'quiet needle':>15}{'address':>10}"
    print(hdr)
    print("-" * len(hdr))
    for arm in (headroom_res, ctx_res):
        ratio = raw_tokens / arm["out_tokens"] if arm["out_tokens"] else float("inf")
        print(f"{arm['tool']:<28}{arm['out_tokens']:>9,}{ratio:>7.1f}×"
              f"{('SURVIVED' if arm['quiet_needle_survived'] else 'DROPPED'):>15}"
              f"{('yes' if arm['retrieval_address'] else 'no'):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
