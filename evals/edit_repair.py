"""How much of a failed host edit is recoverable without guessing.

The field's most-cited harness failure is an edit that does not apply: the
model reproduces a region imperfectly, the exact-substring match misses, and
the turn is spent re-reading and retrying. straitjacket is unusually well
placed to repair that -- its PreToolUse hook already sees every ``Edit`` and
can rewrite the tool's arguments on Claude Code and Codex -- but "well placed"
is not a reason to build. The question is how much of the failure is
*recoverable at all*, and how much of it can only be refused.

This measures that ceiling. It does not, and cannot, measure how often each
failure shape actually occurs; that needs field data, which
``ctx.edit_outcomes`` now records and which this eval deliberately does not
guess at. Read the arms as "if an edit fails THIS way, here is what repair
could do", never as a blended success rate.

    python evals/edit_repair.py            # human-readable receipt
    python evals/edit_repair.py --json     # machine-readable record

Model-free and deterministic: real files from this repository, seeded
sampling, no LLM anywhere. Corpus choice matters -- real indentation, real
repeated boilerplate, and real docstrings are what make uniqueness a
measurement rather than an assumption.
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: Region sizes an agent actually names in an edit: a line or two to retarget,
#: a small block, a function-sized chunk.
SNIPPET_LINES = (2, 6, 15)

_WS_RUN = re.compile(r"[ \t]+")


def _normalize(text: str) -> str:
    """The equivalence a repair is allowed to use: whitespace, and only that.

    Leading indentation, internal spacing and trailing blanks are the parts of
    a region a model routinely fails to reproduce and that carry no meaning a
    reader would defend. Everything else -- identifiers, punctuation, string
    contents, line ORDER -- is left alone, because a repair that matched
    through those would be deciding the edit's target on the strength of a
    resemblance.
    """
    lines = [_WS_RUN.sub(" ", ln.strip()) for ln in text.splitlines()]
    # Blank lines go too. The rule above says "whitespace, and only that", and
    # a blank line is whitespace -- keeping it as content made a dropped blank
    # line unrecoverable while a dropped indent was fine, which is the rule
    # contradicting itself. Safety does not rest on this: a needle that now
    # matches more regions is caught by the candidate COUNT, which refuses.
    return "\n".join(ln for ln in lines if ln).strip()


# --------------------------------------------------------------- corruptions
# Each returns the ``old_string`` a model would emit for a region it read.
def _verbatim(lines):
    return list(lines)


def _reindent(lines):
    """Indentation the model normalized away -- the commonest reproduction
    error, and the one an exact-substring match is least forgiving of."""
    return [ln.lstrip() for ln in lines]


def _respace(lines):
    """Internal spacing tidied: `a  =  1` -> `a = 1`, trailing blanks dropped."""
    return [_WS_RUN.sub(" ", ln).rstrip() for ln in lines]


def _tabs(lines):
    """Leading four-space groups emitted as tabs."""
    out = []
    for ln in lines:
        stripped = ln.lstrip(" ")
        depth = (len(ln) - len(stripped)) // 4
        out.append("\t" * depth + stripped)
    return out


def _drop_blank(lines):
    """Blank lines inside the region elided."""
    kept = [ln for ln in lines if ln.strip()]
    return kept if len(kept) >= 2 else list(lines)


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _paraphrase(lines):
    """A word changed. The region is no longer the region -- the control that
    must NOT be recovered: a repair resolving this one is picking a target the
    model never named.

    Rewrites the first identifier it finds rather than a specific keyword. The
    first cut swapped ``return``->``yield``, which left every region without a
    ``return`` byte-identical to the original -- so two thirds of this arm was
    silently re-running the ``verbatim`` arm and reporting its success as this
    one's. A corruption arm that does not corrupt measures nothing.
    """
    out, done = [], False
    for ln in lines:
        m = _TOKEN.search(ln) if not done else None
        if m:
            out.append(ln[: m.start()] + "zqx" + ln[m.end() :])
            done = True
        else:
            out.append(ln)
    return out


#: Which arms are "the model reproduced the right region imperfectly" versus
#: "the model named a different region". They must be reported apart: a single
#: blended rate over them is governed by how many arms of each kind this file
#: happens to define, which is an authoring choice, not a property of anything.
WHITESPACE_ARMS = ("reindent", "respace", "tabs-for-spaces", "blank-lines-dropped")
CONTENT_ARMS = ("word-changed",)

CORRUPTIONS = (
    ("verbatim", _verbatim),
    ("reindent", _reindent),
    ("respace", _respace),
    ("tabs-for-spaces", _tabs),
    ("blank-lines-dropped", _drop_blank),
    ("word-changed", _paraphrase),
)


def _corpus(limit: int) -> list[tuple[str, list[str]]]:
    out = []
    for f in sorted((ROOT / "src" / "ctx").rglob("*.py")):
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) >= 120:
            out.append((str(f.relative_to(ROOT)), lines))
    return out[:limit]


def _exact_matches(haystack: str, needle: str) -> int:
    """Occurrences of an exact substring -- what the host's edit tool counts.

    Overlap-free scan, matching the semantics of the tools being modelled:
    they count occurrences to decide found / unique / ambiguous.
    """
    if not needle:
        return 0
    count = start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return count
        count += 1
        start = i + len(needle)


def _repair_candidates(file_lines: list[str], needle_lines: list[str]) -> list[int]:
    """Line offsets whose window normalizes to the same text as the needle.

    Windows are tried at the needle's own length and at ±2 lines, because two
    of the corruption shapes (blank lines dropped, and a region the model
    re-wrapped) change the line COUNT while leaving the content recoverable.
    Returns every candidate, so the caller can refuse on more than one -- the
    count is the whole safety property.
    """
    want = _normalize("\n".join(needle_lines))
    if not want:
        return []
    found = []
    n = len(needle_lines)
    for span in {max(1, n - 2), max(1, n - 1), n, n + 1, n + 2}:
        for start in range(0, len(file_lines) - span + 1):
            if _normalize("\n".join(file_lines[start : start + span])) == want:
                found.append(start)
    return sorted(set(found))


def run(seed: int = 20260820, files: int = 40) -> dict:
    rng = random.Random(seed)
    corpus = _corpus(files)
    tally: dict[str, dict[str, int]] = {}

    for path, lines in corpus:
        text = "\n".join(lines)
        for span in SNIPPET_LINES:
            if len(lines) < span + 40:
                continue
            for _ in range(6):
                a = rng.randrange(10, len(lines) - span - 5)
                region = lines[a : a + span]
                if not "\n".join(region).strip():
                    continue
                truth = "\n".join(region)

                for name, corrupt in CORRUPTIONS:
                    row = tally.setdefault(
                        name,
                        {"cases": 0, "not_applicable": 0, "applies_today": 0,
                         "not_found": 0, "not_unique": 0, "repairable": 0,
                         "ambiguous": 0, "unrecoverable": 0, "repair_wrong": 0},
                    )
                    corrupted = corrupt(region)
                    if name != "verbatim" and corrupted == region:
                        # The shape does not apply to this region (nothing to
                        # re-indent, no blank line to drop). Counting it would
                        # be reporting `verbatim`'s result under this arm's
                        # name; declaring it keeps the arm's denominator honest.
                        row["not_applicable"] += 1
                        continue
                    row["cases"] += 1
                    needle = "\n".join(corrupted)

                    hits = _exact_matches(text, needle)
                    if hits == 1:
                        row["applies_today"] += 1
                        continue
                    if hits > 1:
                        # Ambiguous by construction. A repair MUST NOT resolve
                        # this by choosing; counted apart from not_found for
                        # exactly that reason.
                        row["not_unique"] += 1
                        continue
                    row["not_found"] += 1

                    candidates = _repair_candidates(lines, corrupted)
                    if len(candidates) == 1:
                        row["repairable"] += 1
                        # The property that makes repair safe to ship: when it
                        # resolves, it resolves to the region the model was
                        # looking at. One failure here sinks the mechanism.
                        start = candidates[0]
                        recovered = None
                        for width in (span - 2, span - 1, span, span + 1, span + 2):
                            if width < 1:
                                continue
                            window = "\n".join(lines[start : start + width])
                            if _normalize(window) == _normalize(needle):
                                recovered = window
                                break
                        if recovered is not None and _normalize(recovered) != _normalize(truth):
                            row["repair_wrong"] += 1
                    elif len(candidates) > 1:
                        row["ambiguous"] += 1
                    else:
                        row["unrecoverable"] += 1

    total = {k: sum(r[k] for r in tally.values()) for k in next(iter(tally.values()))}
    return {"seed": seed, "files": len(corpus),
            "snippet_lines": list(SNIPPET_LINES), "by_corruption": tally,
            "total": total}


def render(rec: dict) -> str:
    out = [
        f"[edit repair ceiling · {rec['files']} files · "
        f"{rec['total']['cases']} simulated edits · seed {rec['seed']}]",
        "",
        "Each case: a real region, the old_string a model would emit for it,",
        "and what the host's exact-substring edit does with that today.",
        "",
        f"{'how the model got it wrong':<26} {'cases':>6} {'applies':>8} "
        f"{'notfound':>9} {'multi':>6} {'→repair':>8} {'→ambig':>7} "
        f"{'→gone':>6} {'wrong':>6}",
    ]
    for name, r in rec["by_corruption"].items():
        out.append(
            f"{name:<26} {r['cases']:>6} {r['applies_today']:>8} {r['not_found']:>9} "
            f"{r['not_unique']:>6} {r['repairable']:>8} {r['ambiguous']:>7} "
            f"{r['unrecoverable']:>6} {r['repair_wrong']:>6}"
        )
    t = rec["total"]

    def _sum(arms, key):
        return sum(rec["by_corruption"].get(a, {}).get(key, 0) for a in arms)

    ws_fail = _sum(WHITESPACE_ARMS, "not_found")
    ws_ok = _sum(WHITESPACE_ARMS, "repairable")
    ws_amb = _sum(WHITESPACE_ARMS, "ambiguous")
    ws_gone = _sum(WHITESPACE_ARMS, "unrecoverable")
    ct_fail = _sum(CONTENT_ARMS, "not_found")
    ct_ok = _sum(CONTENT_ARMS, "repairable")

    out += [
        "",
        "Read the arms separately. There is no blended success rate here: how "
        "often each",
        "shape actually occurs is field data (ctx.edit_outcomes records it), "
        "not something",
        "this file can know, and averaging the arms would just report how many "
        "of each",
        "kind happen to be written above.",
        "",
        f"the model reproduced the right region imperfectly ({ws_fail} failures)",
    ]
    if ws_fail:
        out += [
            f"  repair resolves      {ws_ok:>5}  ({100 * ws_ok / ws_fail:.1f}%)",
            f"  refuses as ambiguous {ws_amb:>5}  ({100 * ws_amb / ws_fail:.1f}%)",
            f"  cannot find          {ws_gone:>5}  ({100 * ws_gone / ws_fail:.1f}%)",
        ]
    else:
        out.append("  (no cases)")
    out += [
        "",
        f"the model named different content ({ct_fail} failures) — the control",
        f"  repair resolves      {ct_ok:>5}  (must be 0)",
        "",
        f"exact-substring matching already failed {t['not_unique']} times on a "
        "PERFECT reproduction,",
        "because the region occurs more than once. Repair cannot help there and "
        "must not try:",
        "several equally good matches is the model's ambiguity, not a lookup "
        "problem.",
        "",
        f"repairs that landed on the wrong region: {t['repair_wrong']} of "
        f"{t['repairable']}",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    record = run()
    print(json.dumps(record, indent=2, sort_keys=True) if "--json" in sys.argv
          else render(record))
