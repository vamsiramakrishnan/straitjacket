"""What does hand-rolling `ctx refs` cost, measured against a real indexer?

`ctx refs` has an engine ladder: SCIP (compiler-backed, exact) → jedi
(semantic, Python) → a word-boundary regex over source files. The regex is the
floor, and on every language without a semantic rung it is also the ceiling.

A regex cannot tell a reference from the same letters in a comment, a doc
example, a string literal, or an unrelated symbol that happens to share a
name. That is not a tuning problem — the information is not in the text. This
eval prices it, by scoring the textual engine against a SCIP index generated
from the same tree by the language's own compiler front end.

The comparison is only meaningful where a real index exists, so the index is
the ground truth and the run is skipped where one cannot be built. There is no
model, no corpus and no annotation: both sides are computed from the same
checkout, and one of them is the compiler's own answer.

Usage
-----
    ctx index --emit /path/to/checkout      # or: rust-analyzer scip .
    python evals/refs_precision.py --repo /path/to/checkout
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

#: A name occurring this often is usually a language keyword-ish token
#: (`index`, `test`, `len`) whose textual matches are dominated by unrelated
#: definitions. Kept out of the sample so the result is not flattered.
_MAX_OCCURRENCES = 200
_MIN_OCCURRENCES = 3


def _sample_symbols(ws, limit: int) -> list[str]:
    from ctx import scip_ingest

    index = scip_ingest.find_index(ws)
    if index is None:
        return []
    counts: Counter[str] = Counter()
    defined: set[str] = set()
    for occ in scip_ingest.iter_occurrences(index):
        if not occ.name:
            continue
        counts[occ.name] += 1
        if occ.is_definition:
            defined.add(occ.name)
    ok = [
        n
        for n, c in counts.items()
        if n in defined and _MIN_OCCURRENCES <= c <= _MAX_OCCURRENCES
    ]
    return sorted(ok)[:limit]


def _prf(got: set, truth: set) -> tuple[float, float]:
    if not got:
        return (1.0 if not truth else 0.0), (1.0 if not truth else 0.0)
    p = len(got & truth) / len(got)
    r = len(got & truth) / len(truth) if truth else 1.0
    return p, r


def probe_repo(root: Path, *, limit: int) -> dict:
    from ctx import scip_ingest
    from ctx.codeverbs import _ast_refs
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(root))
    store = Store(ws.workspace_id)
    if scip_ingest.find_index(ws) is None:
        return {"root": str(root), "skipped": "no SCIP index — run `ctx index` first"}

    from ctx.codeverbs import resolve_refs

    rows = []
    engines: Counter[str] = Counter()
    for sym in _sample_symbols(ws, limit):
        truth = {(f, ln) for f, ln, _ in (scip_ingest.refs(ws, sym) or [])}
        if not truth:
            continue
        # The floor, always, and separately what the verb actually answers —
        # measuring only the internal would hide whether the ladder reaches
        # the exact rung at all, which was the real defect.
        textual = {(f, ln) for f, ln, _ in _ast_refs(store, ws, sym, None)[0]}
        got_sites, label = resolve_refs(store, ws, sym)
        engines[label] += 1
        served = {(f, ln) for f, ln, _ in got_sites}
        p, r = _prf(textual, truth)
        sp, sr = _prf(served, truth)
        rows.append(
            {
                "symbol": sym,
                "truth": len(truth),
                "textual": len(textual),
                "served": len(served),
                "spurious": len(textual - truth),
                "missed": len(truth - textual),
                "precision": round(p, 4),
                "recall": round(r, 4),
                "served_precision": round(sp, 4),
                "served_recall": round(sr, 4),
            }
        )
    store.close()
    if not rows:
        return {"root": str(root), "skipped": "no comparable symbols"}

    n = len(rows)
    return {
        "root": str(root),
        "symbols": n,
        "macro_precision": round(sum(x["precision"] for x in rows) / n, 4),
        "macro_recall": round(sum(x["recall"] for x in rows) / n, 4),
        "total_truth": sum(x["truth"] for x in rows),
        "total_textual": sum(x["textual"] for x in rows),
        "total_spurious": sum(x["spurious"] for x in rows),
        "total_missed": sum(x["missed"] for x in rows),
        "served_precision": round(sum(x["served_precision"] for x in rows) / n, 4),
        "served_recall": round(sum(x["served_recall"] for x in rows) / n, 4),
        "total_served": sum(x["served"] for x in rows),
        "engines": dict(engines),
        "worst": sorted(rows, key=lambda x: x["precision"])[:8],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", action="append", default=[], required=True)
    ap.add_argument("--limit", type=int, default=60, help="symbols sampled per repo")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    records = [probe_repo(Path(r).resolve(), limit=args.limit) for r in args.repo]
    for rec in records:
        print(f"\n== {rec['root']} ==")
        if "skipped" in rec:
            print(f"  skipped: {rec['skipped']}")
            continue
        print(
            f"  symbols {rec['symbols']} · scip sites {rec['total_truth']} · "
            f"textual sites {rec['total_textual']}"
        )
        print(
            f"  textual vs compiler truth: precision {rec['macro_precision']:.0%} · "
            f"recall {rec['macro_recall']:.0%}"
        )
        print(
            f"  {rec['total_spurious']} sites the regex invented · "
            f"{rec['total_missed']} it missed"
        )
        eng = ",".join(f"{k}:{v}" for k, v in sorted(rec["engines"].items()))
        print(
            f"  what `ctx refs` actually served: precision "
            f"{rec['served_precision']:.0%} · recall {rec['served_recall']:.0%} · "
            f"{rec['total_served']} sites · engines {eng}"
        )
        print("  worst precision:")
        for w in rec["worst"]:
            print(
                f"    {w['symbol']:<28} {w['precision']:>5.0%}  "
                f"({w['textual']} reported, {w['truth']} real)"
            )

    if args.json:
        Path(args.json).write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
