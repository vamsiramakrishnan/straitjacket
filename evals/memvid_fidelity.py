#!/usr/bin/env python3
"""Is memvid's `.mv2` a byte-exact store, or an index over one?

We want a portable evidence capsule: one file a reviewer opens offline in
which every `run:`/`blob:` handle a pull request cites resolves to the exact
bytes that produced it. memvid (https://github.com/memvid/memvid) offers a
single-file, serverless memory format with an embedded write-ahead log,
append-only checksummed frames and a BM25 index — the right container shape
on paper.

straitjacket's rule for a third-party mechanism is the same every time: it
may index, rank, or carry evidence, but it may only be the source of truth
if it returns the original bytes unchanged. This script answers that one
question and nothing else. It calls no model and reaches no network.

    pip install memvid-sdk
    python evals/memvid_fidelity.py

What it does: `put()` a payload, `commit()`, then walk the frames back out
with `blob()` and compare to the bytes that went in. It reports, per case,
how many content pages the payload produced and whether their concatenation
is the original, the original minus trailing newlines, or neither.
Deterministic; no seed needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import tempfile
from typing import Any

#: A record frame whose blob is empty sits at id 0; content pages start at 1.
FIRST_CONTENT_FRAME = 1
#: Stop scanning after this many consecutive missing frame ids. The timeline
#: lists only the logical record, so the pages have to be walked by id.
SCAN_STOP = 1


def cases() -> list[tuple[str, str, str]]:
    """(name, payload, what the case is probing)."""
    nl = "\n"
    return [
        ("tiny", "alpha bravo charlie delta echo",
         "a payload smaller than one page"),
        ("no-trailing-newline", "x" * 5000,
         "a mid-size payload that ends without a newline"),
        ("one-trailing-newline", "".join(f"line {i}{nl}" for i in range(2000)),
         "a log-shaped payload, the usual case"),
        ("two-trailing-newlines", "".join(f"line {i}{nl}" for i in range(2000)) + nl,
         "whether a second trailing newline survives"),
        ("blank-lines-inside", ("a" + nl * 3 + "b" + nl) * 400,
         "runs of blank lines in the body"),
        ("crlf", "".join(f"line {i}\r{nl}" for i in range(2000)),
         "carriage returns"),
        ("trailing-spaces", "".join(f"line {i}   {nl}" for i in range(2000)),
         "trailing whitespace on every line"),
    ]


def roundtrip(mv_mod: Any, workdir: pathlib.Path, name: str, payload: str) -> dict[str, Any]:
    path = workdir / f"{name}.mv2"
    if path.exists():
        path.unlink()
    mv = mv_mod.create(str(path))
    try:
        mv.put(title=name, text=payload)
        mv.commit()
        pages: list[str] = []
        misses = 0
        fid = FIRST_CONTENT_FRAME
        while misses < SCAN_STOP:
            try:
                pages.append(mv.blob(f"mv2://frames/{fid}").decode("utf-8"))
            except Exception:
                misses += 1
            fid += 1
        # What `find` hands back, for contrast: a ranked window, not the bytes.
        hits = (mv.find(payload.strip().split("\n")[0][:24] or "a", k=20) or {}).get("hits") or []
        found = "".join(h.get("text") or "" for h in hits)
        verify = mv.verify(deep=True)
    finally:
        mv.close()

    cat = "".join(pages)
    checks = verify.get("checks") or []
    return {
        "case": name,
        "in_bytes": len(payload.encode("utf-8")),
        "pages": len(pages),
        "page_concat_bytes": len(cat.encode("utf-8")),
        "exact": cat == payload,
        "exact_after_rstrip_newlines": cat == payload.rstrip("\n"),
        "find_text_exact": found == payload,
        "sha_in": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "sha_pages": hashlib.sha256(cat.encode("utf-8")).hexdigest()[:16],
        "file_bytes": path.stat().st_size,
        "verify_failed": [c.get("name") for c in checks if c.get("status") != "passed"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine record on stdout")
    ap.add_argument("--out", default=None, help="also write the record here")
    ns = ap.parse_args()

    try:
        import memvid_sdk
    except ImportError:
        raise SystemExit("needs the memvid SDK: pip install memvid-sdk")

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="sj-memvid-"))
    try:
        rows = [roundtrip(memvid_sdk, workdir, name, payload) for name, payload, _ in cases()]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    why = {name: note for name, _payload, note in cases()}
    payload = {"schema": "sj.memvid_fidelity/v1", "rows": rows}
    if ns.out:
        pathlib.Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(ns.out).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    if ns.json:
        print(json.dumps(payload, indent=1))
        return 0

    print("memvid `.mv2` round trip: bytes in, bytes back out\n")
    print("| case | what it probes | in | pages | back | verdict |")
    print("|---|---|---:|---:|---:|---|")
    for r in rows:
        if r["exact"]:
            verdict = "exact"
        elif r["pages"] == 0:
            verdict = "**no byte path** (no content page)"
        elif r["exact_after_rstrip_newlines"]:
            verdict = "**trailing newline(s) lost**"
        else:
            verdict = "**differs**"
        print(f"| {r['case']} | {why[r['case']]} | {r['in_bytes']:,} | {r['pages']} | "
              f"{r['page_concat_bytes']:,} | {verdict} |")

    exact = sum(1 for r in rows if r["exact"])
    nopath = sum(1 for r in rows if r["pages"] == 0)
    print(f"\n{exact}/{len(rows)} cases round-trip byte-exact; {nopath} have no byte path at all.")
    print("`find` returned the payload verbatim in "
          f"{sum(1 for r in rows if r['find_text_exact'])}/{len(rows)} cases — it is a ranked "
          "window, not a retrieval address.")
    bad = [r["case"] for r in rows if r["verify_failed"]]
    print(f"`verify(deep=True)` passed every check in {len(rows) - len(bad)}/{len(rows)} cases"
          + (f"; failures: {bad}" if bad else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
