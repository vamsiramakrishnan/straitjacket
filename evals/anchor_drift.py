"""Measure how often a ``repo:`` line address stops meaning what it meant.

Why this exists: straitjacket's central claim is that omission is reversible --
*"the same address returns the same bytes tomorrow, next week, on another
machine."* That is enforced and measured for the immutable side of the store.
It was never measured for ``repo:`` addresses, which are line numbers into a
file the agent is concurrently editing, and where the claim is simply false: a
line number is a position, not an identity.

The question this instrument answers is not "does anchoring work" -- the unit
tests pin that. It is **how large the exposure was**, in the only units that
matter to a reader: how often a re-resolved address silently returns different
code, how much of that an anchor converts into a correct answer rather than
merely a caught error, and what the guarantee costs in characters.

Deliberately model-free. It replays real edit shapes over real files from this
repository and compares resolutions; nothing here calls an LLM, so the numbers
are reproducible in a review sandbox and cannot drift with a vendor's weights.

    python evals/anchor_drift.py            # human-readable receipt
    python evals/anchor_drift.py --json     # machine-readable record

The corpus is this repository's own ``src/ctx/*.py``: files with real
indentation, real duplicate lines, and real docstrings, which is what makes
relocation a measurement rather than a demonstration.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from ctx import anchors  # noqa: E402

#: Spans an agent actually asks for: a couple of lines it is about to change,
#: a small block, a function-sized region.
SPAN_LENGTHS = (2, 8, 25)

#: Edit shapes, as a function of the file's lines. Each returns the edited file
#: and a label. These are the mutations that make a stored line address wrong:
#: anything that changes how many lines sit ABOVE the address.
def _insert_above(lines, at, n=3):
    return lines[:at] + [f"# inserted {i}" for i in range(n)] + lines[at:]


def _delete_above(lines, at, n=3):
    return lines[: max(0, at - n)] + lines[at:]


def _move_block(lines, a, b):
    """Relocate the span itself to the end of the file -- content intact, all
    coordinates wrong. The case a whole-file generation guard cannot tell from
    a deletion, and the one an anchor answers exactly."""
    block = lines[a:b]
    return lines[:a] + lines[b:] + [""] + block


def _rewrite_span(lines, a, b):
    """The span's own content changes. Nothing can find it; the only correct
    behaviours are 'refuse' or 'silently answer the wrong question'."""
    return lines[:a] + [f"# rewritten {i}" for i in range(b - a)] + lines[b:]


def _corpus(limit: int) -> list[tuple[str, list[str]]]:
    files = sorted((ROOT / "src" / "ctx").rglob("*.py"))
    out = []
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) >= 120:
            out.append((str(f.relative_to(ROOT)), lines))
    return out[:limit]


def _resolve_unanchored(after: list[str], a: int, b: int) -> list[str]:
    """What ``ctx get repo:f --lines a:b`` returns after the edit: whatever now
    occupies those coordinates, clamped to the file. No check, exit 0."""
    return after[a - 1 : b]


def _resolve_anchored(after: list[str], a: int, b: int, want: str):
    """The three outcomes, in the order ``ctx get`` decides them."""
    window = after[a - 1 : b] if 1 <= a <= len(after) else []
    if len(window) == b - a + 1 and anchors.anchor(window) == want:
        return "verified", window
    moved = anchors.relocate(after, want, b - a + 1, a)
    if moved is None:
        return "refused", None
    return "relocated", after[moved - 1 : moved + (b - a)]


def run(seed: int = 20260820, files: int = 40) -> dict:
    rng = random.Random(seed)
    corpus = _corpus(files)
    cases = []
    for path, lines in corpus:
        for span_len in SPAN_LENGTHS:
            if len(lines) < span_len + 40:
                continue
            for _ in range(4):
                a = rng.randrange(20, len(lines) - span_len - 5)
                b = a + span_len - 1
                truth = lines[a - 1 : b]
                want = anchors.anchor(truth)
                edit_at = rng.randrange(1, a - 1)
                for label, after in (
                    ("insert-above", _insert_above(lines, edit_at)),
                    ("delete-above", _delete_above(lines, edit_at)),
                    ("move-the-span", _move_block(lines, a - 1, b)),
                    ("rewrite-the-span", _rewrite_span(lines, a - 1, b)),
                ):
                    cases.append((path, label, a, b, truth, want, after))

    tally: dict[str, dict[str, int]] = {}
    anchor_chars = 0
    address_chars = 0
    for path, label, a, b, truth, want, after in cases:
        row = tally.setdefault(
            label,
            {"cases": 0, "unanchored_silently_wrong": 0, "unanchored_correct": 0,
             "verified": 0, "relocated": 0, "refused": 0, "anchored_wrong": 0},
        )
        row["cases"] += 1

        got = _resolve_unanchored(after, a, b)
        if got == truth:
            row["unanchored_correct"] += 1
        else:
            row["unanchored_silently_wrong"] += 1

        outcome, payload = _resolve_anchored(after, a, b, want)
        row[outcome] += 1
        # The property that makes the mechanism trustworthy: when an anchored
        # read ANSWERS, the answer is the content the address named. A single
        # count here would be a defect, not a statistic.
        if payload is not None and payload != truth:
            row["anchored_wrong"] += 1

        bare = f"repo:{path} --lines {a}:{b}"
        address_chars += len(bare)
        anchor_chars += len(anchors.format_span(a, b, want)) - len(f"{a}:{b}")

    total = {k: sum(r[k] for r in tally.values()) for k in next(iter(tally.values()))}
    return {
        "seed": seed,
        "files": len(corpus),
        "span_lengths": list(SPAN_LENGTHS),
        "by_edit": tally,
        "total": total,
        "cost": {
            "bare_address_chars": address_chars,
            "anchor_chars_added": anchor_chars,
            "overhead_pct": round(100 * anchor_chars / address_chars, 1),
        },
    }


def render(rec: dict) -> str:
    t = rec["total"]
    n = t["cases"]
    out = [
        f"[anchor drift · {rec['files']} files · {n} resolutions · seed {rec['seed']}]",
        "",
        "Every case: an address minted before an edit, re-resolved after it.",
        "",
        f"{'edit shape':<18} {'cases':>6} {'silently wrong':>15} {'verified':>9} "
        f"{'relocated':>10} {'refused':>8} {'wrong answer':>13}",
    ]
    for label, r in rec["by_edit"].items():
        out.append(
            f"{label:<18} {r['cases']:>6} {r['unanchored_silently_wrong']:>15} "
            f"{r['verified']:>9} {r['relocated']:>10} {r['refused']:>8} "
            f"{r['anchored_wrong']:>13}"
        )
    out += [
        f"{'ALL':<18} {n:>6} {t['unanchored_silently_wrong']:>15} {t['verified']:>9} "
        f"{t['relocated']:>10} {t['refused']:>8} {t['anchored_wrong']:>13}",
        "",
        f"unanchored: {100 * t['unanchored_silently_wrong'] / n:.1f}% of re-resolutions "
        "returned different content, exit 0, no note",
        f"anchored:   {100 * (t['verified'] + t['relocated']) / n:.1f}% answered correctly "
        f"({t['relocated']} of them by following content that moved), "
        f"{100 * t['refused'] / n:.1f}% refused, "
        f"{t['anchored_wrong']} wrong answers",
        f"cost:       +{rec['cost']['anchor_chars_added']} characters over "
        f"{rec['cost']['bare_address_chars']} of address "
        f"({rec['cost']['overhead_pct']}%)",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    record = run()
    if "--json" in sys.argv:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(render(record))
