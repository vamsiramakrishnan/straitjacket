#!/usr/bin/env python3
"""Combine per-arm `iterative_harness.py` runs into one comparison table.

Each arm is run separately (one process, its own throwaway repo), so the
comparison lives in the run directories rather than in a single record. This
reads them and emits the table the receipt quotes: score per phase, fix rounds,
billed cost, and — the number that turns out to matter — how many input tokens
each arm's builder had re-sent to it.

Usage:
  python evals/vibecode/combine_arms.py evals/_runs/iter3-solo evals/_runs/iter4-cross ...
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent / "src"))

LABEL = {
    "solo": "one frontier model does everything",
    "orchestrated": "plan → Opus, build → Sonnet",
    "cross": "plan → Opus, build → Antigravity (Gemini)",
    "cross-sj": "plan → Opus, build → Antigravity, **contained**",
}


def load(dirs: list[str]) -> list[dict]:
    out = []
    for d in dirs:
        f = pathlib.Path(d) / "records.json"
        if f.is_file():
            out += json.loads(f.read_text())
    return out


def _builder_input(rec: dict) -> int | None:
    """Input tokens re-sent to the *build* nodes.

    Reported only for the Antigravity arms. The two vendors are not measuring
    the same thing: the Antigravity SDK's `prompt_token_count` is the whole
    prompt, while the Claude CLI splits its input across `input_tokens`,
    `cache_creation_input_tokens` and `cache_read_input_tokens` — so putting
    them in one column would compare a total against a sliver. Cost is the
    cross-vendor number; this column is the like-for-like containment
    measurement between `cross` and `cross-sj`.
    """
    if not rec["arm"].startswith("cross"):
        return None
    return sum(u.get("input", 0) or 0 for u in rec["usage"]
               if u["engine"].startswith("antigravity/"))


def render(recs: list[dict]) -> str:
    order = {a: i for i, a in enumerate(LABEL)}
    recs = sorted(recs, key=lambda r: order.get(r["arm"], 99))
    lines = [
        "| arm | split | phase 1 | phase 2 | phase 3 | mean | fix | Gemini input tok "
        "| billed |",
        "|---|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for r in recs:
        ps = r["phases"]
        cells = " | ".join(f"{p['passed']}/{p['total']}" for p in ps)
        tin = _builder_input(r)
        lines.append(
            f"| `{r['arm']}` | {LABEL.get(r['arm'], '')} | {cells} | "
            f"{r['score']*100:.0f}% | {'+'.join(str(p['fix_rounds']) for p in ps)} | "
            f"{'—' if tin is None else format(tin, ',')} | ${r['cost_usd']:.2f} |"
        )
    lines.append("")
    lines.append("_Gemini input tokens are shown only for the Antigravity arms; the "
                 "Claude CLI splits its input across uncached / cache-write / "
                 "cache-read categories, so the two vendors' token counts are not one "
                 "column. Cost is the cross-vendor comparison._")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out")
    ns = ap.parse_args()
    table = render(load(ns.dirs))
    if ns.out:
        pathlib.Path(ns.out).write_text(table + "\n", "utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
