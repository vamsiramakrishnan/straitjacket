#!/usr/bin/env python3
"""Write the film's data from the repository's own receipts, so that no number on the page is typed by hand.

    python site/film/tools/make_data.py            # write site/public/film/data/film.json and lines.bin
    python site/film/tools/make_data.py --check    # fail if those files are not what this script writes

film.json   every number and specimen the film shows, each beside the repository file it came from
lines.bin   two bytes per line of the 20,001-line fixture, in log order: template family, length in characters

The fixture log is rebuilt with the algorithm of evals/field_needle.py (build_corpus, seed 1234). The script stops
unless the result has the SHA-256 recorded in evals/field-needle-record.json and the template counts printed in
tests/fixtures/field_needle_main_digest.txt, so the film cannot show a log other than the receipt's.
The output holds no timestamp, commit or host path: the same receipts give the same bytes.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "site" / "public" / "film" / "data"
GITHUB = "https://github.com/vamsiramakrishnan/straitjacket/blob/main/"
SEED = 1234
LOUD = (5000, 17650)
VERBS = ["accepted", "processed", "forwarded", "cached", "validated", "dispatched", "completed", "enqueued"]


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def build() -> tuple[str, bytes]:
    record = json.loads(read("evals/field-needle-record.json"))
    anchor = json.loads(read("evals/anchor-drift-2026-08-20.json"))
    digest = read("tests/fixtures/field_needle_main_digest.txt")
    corpus = record["corpus"]
    total, needle = corpus["lines"], corpus["quiet_needle_line"]

    # The log, line for line: the same draws from the same generator as evals/field_needle.py::build_corpus.
    rng = random.Random(SEED)
    workers = [f"worker-{i}" for i in range(1, 33)]
    lines: list[str] = []
    kind: list[str] = []
    for i in range(1, total + 1):
        if i == needle:
            lines.append(f"INFO {rng.choice(workers)} checkout request req-14237 fell back to legacy gateway after circuit opened")
            kind.append("needle")
        elif i == LOUD[0]:
            lines.append("ERROR db-pool connection refused on shard 7 (attempt 3)")
            kind.append("loud0")
        elif i == LOUD[1]:
            lines.append("ERROR cache eviction storm: 4128 keys dropped in 200ms window")
            kind.append("loud1")
        else:
            worker, verb, ms = rng.choice(workers), rng.choice(VERBS), rng.randint(2, 90)
            lines.append(f"INFO {worker} request req-{i:05d} {verb} in {ms}ms")
            kind.append(verb)
    text = "\n".join(lines) + "\n"
    sha = hashlib.sha256(text.encode()).hexdigest()
    if sha != corpus["sha256"] or len(text.encode()) != corpus["bytes"]:
        raise SystemExit(f"the rebuilt log is not the receipt's log: sha256 {sha}, the receipt says {corpus['sha256']}")

    # The census, as the checked-in digest prints it.
    families = []
    for m in re.finditer(r"^\s+([\d,]+)× (L\d+): (.+?)(?: · span [0-9a-f]+)?$", digest, re.M):
        verb = next((v for v in VERBS if f" {v} in " in m.group(3)), None)
        if verb:
            families.append({"verb": verb, "count": int(m.group(1).replace(",", "")), "first_line": m.group(2)})
    for family in families:
        if kind.count(family["verb"]) != family["count"]:
            raise SystemExit(f"the rebuilt log has {kind.count(family['verb'])} '{family['verb']}' lines; the digest says {family['count']}")
    order = [f["verb"] for f in families] + ["loud0", "needle", "loud1"]          # family ids 0..10
    binary = bytes(b for k, s in zip(kind, lines) for b in (order.index(k), min(len(s), 255)))

    arms = {a["tool"]: a for a in record["arms"]}
    keep, cut = arms["naive (raw flood)"], arms["caveman (head+tail trunc)"]
    ours = next(a for name, a in arms.items() if name.startswith("sj "))
    handle = re.search(r"\[ctx (run:[0-9a-f]+)", digest).group(1)
    covered = re.search(r"templates: (\d+) cover ([\d,]+)/([\d,]+) lines", digest)
    shown = re.search(r"shown: (\d+) spans · omitted: ([\d,]+) lines", digest)
    total_of = anchor["total"]
    film = {
        "source": {"repository": "vamsiramakrishnan/straitjacket", "blob": GITHUB},
        "log": {
            "lines": total, "bytes": corpus["bytes"], "sha256": corpus["sha256"], "raw_tokens": corpus["raw_tokens_o200k"],
            "needle_line": needle, "needle_text": lines[needle - 1], "loud_lines": list(LOUD),
            "loud_text": [lines[LOUD[0] - 1], lines[LOUD[1] - 1]], "sample": lines[:6],
            "families": [{"id": i, **f} for i, f in enumerate(families)], "family_order": order,
            "receipt": "evals/field-needle-record.json", "generator": "evals/field_needle.py",
        },
        "fates": {
            "keep_everything": {"tokens": keep["out_tokens"], "needle_kept": keep["quiet_needle_survived"]},
            "head_and_tail": {"tokens": cut["out_tokens"], "head_lines": 40, "tail_lines": 40, "needle_kept": cut["quiet_needle_survived"]},
            "digest": {"tokens": ours["out_tokens"], "needle_kept": ours["quiet_needle_survived"], "address": ours["retrieval_address"]},
            "receipt": "evals/field-needle-record.json",
        },
        "digest": {
            "text": digest, "handle": handle, "lines": digest.rstrip("\n").count("\n") + 1, "templates": int(covered.group(1)),
            "covered": int(covered.group(2).replace(",", "")), "spans_shown": int(shown.group(1)),
            "lines_omitted": int(shown.group(2).replace(",", "")), "receipt": "tests/fixtures/field_needle_main_digest.txt",
        },
        "residency": {
            "illustrative": True, "turns": 7, "resident_turns": 6, "token_turns": corpus["raw_tokens_o200k"] * 6,
            "note": "Illustrative seven-turn trace; payload and digest sizes from evals/field-needle-record.json",
            "source": "assets/readme/diagrams/ae-residency.svg",
        },
        "anchors": {
            "cases": total_of["cases"], "relocated": total_of["relocated"], "verified": total_of["verified"],
            "refused": total_of["refused"], "anchored_wrong": total_of["anchored_wrong"],
            "unanchored_silently_wrong": total_of["unanchored_silently_wrong"], "files": anchor["files"],
            "overhead_pct": anchor["cost"]["overhead_pct"], "receipt": "evals/anchor-drift-2026-08-20.json",
        },
    }
    # The head-and-tail arm keeps 40 + 40 lines in evals/field_needle.py; fail here if that ever changes.
    source = read("evals/field_needle.py")
    if "CAVEMAN_HEAD = 40" not in source or "CAVEMAN_TAIL = 40" not in source:
        raise SystemExit("evals/field_needle.py no longer keeps 40 head and 40 tail lines; update the film")
    return json.dumps(film, indent=1, ensure_ascii=False) + "\n", binary


def main() -> int:
    text, binary = build()
    targets = ((OUT / "film.json", text.encode("utf-8")), (OUT / "lines.bin", binary))
    if "--check" in sys.argv:
        stale = [str(path.relative_to(REPO)) for path, data in targets if not path.is_file() or path.read_bytes() != data]
        if stale:
            print("stale film data (run site/film/tools/make_data.py): " + ", ".join(stale))
            return 1
        print("film data is current: film.json, lines.bin")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for path, data in targets:
        path.write_bytes(data)
        print("wrote", path.relative_to(REPO), len(data), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
