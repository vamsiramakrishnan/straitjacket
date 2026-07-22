#!/usr/bin/env python3
"""M-K4 referee: what precision does SCIP add over jedi/ast for references?

The claim (docs/SUBSTRATE.md §M-K4): SCIP resolves references across files
with compiler/type precision — the tier jedi (semantic, best-effort) and
ast (textual) approximate. This measures it deterministically on a fixture
project with a known ground-truth reference set for one symbol, comparing
what each rung of the shipped ladder recovers.

Ground truth — the REAL function references of pkg.core.helper (the
fixture's main.py also carries decoys: a comment, a string, and a
shadowing local, all containing the token "helper"):
  pkg/core.py:1   def helper(x)                    (definition)
  pkg/core.py:6   return helper(41)                (call in use_helper)
  main.py:1       from pkg.core import helper      (import)
  main.py:12      print(helper(1))                 (call)
Decoys (a precise engine must NOT return these):
  main.py:3 (comment) · main.py:4 (string) · main.py:8/9 (shadowing local)

Usage: python3 evals/scip_precision.py [--json]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

GROUND_TRUTH = {
    ("pkg/core.py", 1),
    ("pkg/core.py", 6),
    ("main.py", 1),
    ("main.py", 12),
}
DECOYS = {("main.py", 3), ("main.py", 4), ("main.py", 8), ("main.py", 9)}

_MAIN_PY = (
    "from pkg.core import helper\n\n"
    "# helper is the tenant helper described in the docs\n"
    'note = "remember to call helper before commit"\n\n\n'
    "def local_shadow():\n"
    '    helper = "shadowed string, not the function"\n'
    "    return helper\n\n\n"
    "print(helper(1))\n"
)


def _fixture(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef use_helper():\n"
        "    return helper(41)\n", encoding="utf-8",
    )
    (root / "main.py").write_text(_MAIN_PY, encoding="utf-8")
    src = Path(__file__).resolve().parent.parent / "tests/fixtures/scip_sample.scip"
    shutil.copy(src, root / "index.scip")


def _recall(coords: set) -> float:
    return len(coords & GROUND_TRUTH) / len(GROUND_TRUTH)


def _precision(coords: set) -> float:
    return len(coords & GROUND_TRUTH) / len(coords) if coords else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from ctx import codeverbs, scip_ingest
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    root = Path(tempfile.mkdtemp()) / "proj"
    root.mkdir()
    _fixture(root)
    ws = resolve_workspace(str(root))
    store = Store(ws.workspace_id)

    def coords(sites) -> set:
        return {(f, ln) for f, ln, _ in (sites or [])}

    # SCIP rung (index present).
    scip = coords(scip_ingest.refs(ws, "helper"))
    # jedi/ast rung (force the fallback by hiding the index from the ladder).
    index = root / "index.scip"
    index.rename(root / "index.scip.bak")
    fallback_sites, fallback_engine = codeverbs.resolve_refs(store, ws, "helper")
    fb = coords(fallback_sites)
    (root / "index.scip.bak").rename(index)

    result = {
        "symbol": "helper",
        "ground_truth": sorted(f"{f}:{ln}" for f, ln in GROUND_TRUTH),
        "scip": {
            "engine": "scip (exact)",
            "recall": round(_recall(scip), 2),
            "precision": round(_precision(scip), 2),
            "false_positives": sorted(f"{f}:{ln}" for f, ln in (scip & DECOYS)),
            "found": sorted(f"{f}:{ln}" for f, ln in scip),
        },
        "fallback": {
            "engine": fallback_engine,
            "recall": round(_recall(fb), 2),
            "precision": round(_precision(fb), 2),
            "false_positives": sorted(f"{f}:{ln}" for f, ln in (fb & DECOYS)),
            "found": sorted(f"{f}:{ln}" for f, ln in fb),
        },
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"symbol: helper · ground truth: {len(GROUND_TRUTH)} real refs "
              f"(2 files) + {len(DECOYS)} decoys (comment/string/shadow)")
        for tier in ("scip", "fallback"):
            r = result[tier]
            fp = len(r["false_positives"])
            print(f"  {r['engine']:14s} recall {r['recall']:.0%} · "
                  f"precision {r['precision']:.0%} · {fp} false positive(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
